import argparse
from collections import deque, namedtuple
import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
import vista
from vista.utils import transform
from vista.entities.agents.Dynamics import tireangle2curvature
from vista.utils import logging, misc
import random
import cv2

"""
Creating the D3QN class that splits into two streams: advantage and value
"""
class DuelingDDQN(nn.Module):
    def __init__(self, action_dim):
        super(DuelingDDQN, self).__init__()
        # Convolutional and pooling layers
        self.conv1 = nn.Conv2d(3, 32, kernel_size=8, stride=4)
        self.pool1 = nn.MaxPool2d(kernel_size=2, stride=2)
        self.conv2 = nn.Conv2d(32, 64, kernel_size=4, stride=2)
        self.pool2 = nn.MaxPool2d(kernel_size=2, stride=2)
        self.conv3 = nn.Conv2d(64, 64, kernel_size=3, stride=1)
        self.pool3 = nn.MaxPool2d(kernel_size=2, stride=2)

        # Flatten the output of the final convolutional layer
        self.flatten_size = self._get_conv_output((3, 200, 320))

        # Fully connected layers
        self.fc1 = nn.Linear(self.flatten_size, 512)

        # State Value stream
        self.value_stream = nn.Linear(512, 1)

        # Advantage stream
        self.advantage_stream = nn.Linear(512, action_dim)

    def _get_conv_output(self, shape):
        with torch.no_grad():
            input = torch.zeros(1, *shape)
            output = self.conv3(self.conv2(self.conv1(input)))
            return int(np.prod(output.size()))

    def forward(self, state):
        # Convert state to float and scale if necessary
        state = state.float() / 255.0  # Scale images to [0, 1]

        x = F.relu(self.pool1(self.conv1(state)))
        x = F.relu(self.pool2(self.conv2(x)))
        x = F.relu(self.pool3(self.conv3(x)))

        # Flatten and pass through fully connected layer
        x = x.reshape(x.size(0), -1)
        x = F.relu(self.fc1(x))

        # Value and advantage streams
        value = self.value_stream(x)
        advantage = self.advantage_stream(x)

        # Combine to get Q-values
        q_values = value + advantage - advantage.mean(dim=1, keepdim=True)
        return q_values

"""
Creating the behavior and target neural networks
Initializing the loss function and optimizer
"""
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
behavior_nn = DuelingDDQN(6191).to(device)
target_nn = DuelingDDQN(6191).to(device)

optimizer = torch.optim.Adam(behavior_nn.parameters(), lr=1e-5)
loss_fn = nn.SmoothL1Loss() # huber loss


"""
Defining the environment class
"""
class environment:
    def __init__(
            self,
            trace_paths,
            trace_config,
            car_config,
            sensor_config
    ):
        self.world = vista.World(trace_paths, trace_config)
        self.agent = self.world.spawn_agent(car_config)
        self.agent.spawn_camera(sensor_config)

        self.distance = 0
        self.prev_xy = np.zeros((2, ))

        # creating an action space where curvature ranges from -0.2 to 0.2 and speed ranges from 0 to 15
        curvature_increment = 0.01
        speed_increment = 0.1

        curvature_range = np.arange(-0.2, 0.2+curvature_increment, curvature_increment)
        speed_range = np.arange(0, 15+speed_increment, speed_increment)

        curvature_grid, speed_grid = np.meshgrid(curvature_range, speed_range)

        self.action_space = np.stack([curvature_grid.ravel(), speed_grid.ravel()], axis=1)

    
    def reset(self):
        self.world.reset()
        self.agent = self.world.agents[0]
        observations = self.agent.observations
        self.distance = 0
        self.prev_xy = np.zeros((2, ))
        return observations
    
    def step(self, action, dt = 1/30):
        self.agent.step_dynamics(action, dt=dt)
        self.agent.step_sensors()
        next_state = self.agent.observations

        # Defining conditions for reward function
        road_half_width = self.agent.trace.road_width / 2.
        out_of_lane = np.abs(self.agent.relative_state.x) > road_half_width

        maximal_rotation = np.pi / 10
        exceed_max_rotation = np.abs(self.agent.steering) > maximal_rotation

        done = self.agent.done or out_of_lane or exceed_max_rotation

        # Compute reward
        # reward = -1 if done else 0

        reward = 0
        if out_of_lane and exceed_max_rotation:
            reward = -1
        elif out_of_lane or exceed_max_rotation:
            reward = -0.5
        
        # get other info
        info = misc.fetch_agent_info(self.agent)
        info['out_of_lane'] = out_of_lane
        info['exceed_rot'] = exceed_max_rotation
        
        # Update car ego info
        current_xy = self.agent.ego_dynamics.numpy()[:2]
        self.distance += np.linalg.norm(current_xy - self.prev_xy)
        self.prev_xy = current_xy
        info['distance'] = self.distance

        return next_state, reward, done, info
    

    def epsilon_greedy_action(self, state, epsilon):
        # Restructuring the states to match the input of the conv layers
        state = state.permute(0, 3, 1, 2)
        prob = np.random.uniform()
        if prob < epsilon:
            action_idx = np.random.randint(len(self.action_space))
            return self.action_space[action_idx]
        else:
            qs = behavior_nn.forward(state).cpu().data.numpy()
            action_idx = np.argmax(qs)
            return self.action_space[action_idx]

"""
Replay buffer class
"""
class ReplayBuffer:
    def __init__(self, capacity):
        self.buffer = deque(maxlen=capacity)
    
    def store(self, experience):
        self.buffer.append(experience)
    
    def sample(self, batch_size):
        return random.sample(self.buffer, batch_size)
    
    def size(self):
        return len(self.buffer)


Transition = namedtuple('Transition', ('state', 'action', 'reward', 'next_state', 'done'))

def optimize_model(memory, batch_size, gamma):
    if memory.size() < batch_size:
        return
    
    transitions = memory.sample(batch_size)
    batch = Transition(*zip(*transitions))

    # convert to tensors and move to device
    state_batch = torch.cat([s for s in batch.state]).to(device)
    action_batch = torch.cat([torch.tensor([a]).to(device) for a in batch.action])
    reward_batch = torch.cat([torch.tensor([r]).to(device) for r in batch.reward])
    next_state_batch = torch.cat([s for s in batch.next_state if s is not None]).to(device)
    non_final_mask = torch.tensor(tuple(map(lambda s: s is not None, batch.next_state)), dtype=torch.bool).to(device)

    # Compute Q
    state_action_values = behavior_nn(state_batch).gather(1, action_batch.unsqueeze(1))

    # Compute V
    next_state_values = torch.zeros(batch_size).to(device)
    next_state_values[non_final_mask] = target_nn(next_state_batch).max(1)[0].detach()

    # Compute the expected Q values
    expected_state_action_values = (next_state_values * gamma) + reward_batch

    # Compute Huber loss
    loss = F.smooth_l1_loss(state_action_values, expected_state_action_values.unsqueeze(1))

    # Optimize the model
    optimizer.zero_grad()
    loss.backward()
    optimizer.step()

if __name__ == '__main__':
    """
    Defining environment configurations 
    """
    parser = argparse.ArgumentParser(
        description='Run the simulator with random actions')
    parser.add_argument('--trace-path',
                        type=str,
                        nargs='+',
                        help='Path to the traces to use for simulation')
    args = parser.parse_args()

    trace_config={'road_width': 4}
    car_config={
            'length': 5.,
            'width': 2.,
            'wheel_base': 2.78,
            'steering_ratio': 14.7,
            'lookahead_road': True
        }
    sensor_config={
        'size': (200, 320),
    }

    env = environment(args.trace_path, trace_config, car_config, sensor_config)
    display = vista.Display(env.world)


    """
    Initializing hyper-parameters and beginning the training loop
    """
    replay_buffer = ReplayBuffer(10000)
    batch_size = 128
    gamma = 0.99 
    epsilon_start = 1.0
    epsilon_end = 0.01
    epsilon_decay = 0.995
    num_episodes = 500
    target_update = 10  # Update target network every 10 episodes

    epsilon = epsilon_start
    for episode in range(num_episodes):
        state = env.reset()['camera_front']
        # print(state)
        display.reset()
        total_reward = 0
        done = False

        while not done:
            # Convert state to the appropriate format and move to device
            state_tensor = torch.from_numpy(state).unsqueeze(0).to(device)

            # Select action using epsilon greedy policy
            action = env.epsilon_greedy_action(state_tensor, epsilon)
            next_state, reward, done, _ = env.step(action)
            next_state = next_state['camera_front']

            # Convert next_state to tensor and move to device
            next_state_tensor = torch.from_numpy(next_state).unsqueeze(0).to(device) if next_state is not None else None

            # Store the transition in the replay buffer
            replay_buffer.store((state_tensor, action, reward, next_state_tensor, done))

            state = next_state
            total_reward += reward

            vis_img = display.render()

            # Optimize the model if the replay buffer has enough samples
            optimize_model(replay_buffer, batch_size, gamma)

            cv2.imshow(f'Car Agent in Episode {episode}', vis_img[:, :, ::-1])
            cv2.waitKey(20)

        print(f'Episode {episode}: Total Reward: {total_reward}, Epsilon: {epsilon}')

        # Update epsilon
        epsilon = max(epsilon_end, epsilon_decay * epsilon)

        # Update the target network
        if episode % target_update == 0:
            target_nn.load_state_dict(behavior_nn.state_dict())
    
    # Save the model's state dictionary
    torch.save(behavior_nn.state_dict(), 'behavior_nn_model.pth')


