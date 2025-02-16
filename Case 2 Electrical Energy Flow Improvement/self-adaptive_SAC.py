import wandb
import argparse
import gym
import torch
import torch.nn as nn
from torch.distributions import Categorical
import numpy as np
import torch.nn.functional as F
import torch.optim as optim
from torch.nn.utils import clip_grad_norm_
import copy
from collections import namedtuple
from collections import deque
import random
from tcl_env_dqn_1 import *
from bayes_opt import acquisition
from bayes_opt.util import load_logs
from bayes_opt import BayesianOptimization
from bayes_opt.logger import JSONLogger
from bayes_opt.event import Events

def hidden_init(layer):
  # fuction to initialize weights
  fan_in = layer.weight.data.size()[0]
  lim = 1. / np.sqrt(fan_in)
  return (-lim, lim)

class ReplayBuffer:
    
    def __init__(self, buffer_size, batch_size, device):
        """Initialize the class instance.

            Args:
                buffer_size (int): The maximum size of the memory buffer.
                batch_size (int): The size of the batches used for training data.
                device: The device (e.g., CPU or GPU) on which the instance will operate.
        """
        self.device = device
        self.memory = deque(maxlen=buffer_size)  
        self.batch_size = batch_size
        self.experience = namedtuple("Experience", field_names=["state", "action", "reward", "next_state", "done"])
    
    def add(self, state, action, reward, next_state, done):
        """Add to memory."""
        e = self.experience(state, action, reward, next_state, done)
        self.memory.append(e)
    
    def sample(self):
        """Randomly sample a batch of experiences from memory."""
        experiences = random.sample(self.memory, k=self.batch_size)

        states = torch.from_numpy(np.stack([e.state for e in experiences if e is not None])).float().to(self.device)
        actions = torch.from_numpy(np.vstack([e.action for e in experiences if e is not None])).float().to(self.device)
        rewards = torch.from_numpy(np.vstack([e.reward for e in experiences if e is not None])).float().to(self.device)
        next_states = torch.from_numpy(np.stack([e.next_state for e in experiences if e is not None])).float().to(self.device)
        dones = torch.from_numpy(np.vstack([e.done for e in experiences if e is not None]).astype(np.uint8)).float().to(self.device)
  
        return (states, actions, rewards, next_states, dones)

    def __len__(self):
        return len(self.memory)
    
class Actor(nn.Module):

  def __init__(self, state_size, action_size, hidden_size=32):
        super(Actor, self).__init__()
        self.state_size = state_size
        self.fc1 = nn.Linear(8, hidden_size)
        self.fc2 = nn.Linear(hidden_size, hidden_size)
        self.fc3 = nn.Linear(hidden_size, action_size)
        self.softmax = nn.Softmax(dim=-1)

  def forward(self,state):
        #l_input1 = lambda x: x[:, 0:self.state_size - 7]
        #l_input2 = lambda x: x[:, -7:]
        d = len(state.shape)
        l1,_,l2 = torch.tensor_split(state, (self.state_size-7,-7), dim=d-1)
        l1 = torch.mean(torch.unsqueeze(l1,dim=d-1),dim=d)
        x = torch.cat((l1, l2), dim=d-1)
        x = F.relu(self.fc1(x))
        x = F.relu(self.fc2(x))
        action_probs = self.softmax(self.fc3(x))
        return action_probs

  def evaluate(self, state):
        action_probs = self.forward(state)
        dist = Categorical(action_probs)
        action = dist.sample().to(state.device)
        # Have to deal with situation of 0.0 probabilities because we can't do log 0
        z = action_probs == 0.0
        z = z.float() * 1e-8
        log_action_probabilities = torch.log(action_probs + z)
        return action.detach().cpu(), action_probs, log_action_probabilities
  
  def get_action(self, state):
        action_probs = self.forward(state)

        dist = Categorical(action_probs)
        action = dist.sample().to(state.device)
        # Have to deal with situation of 0.0 probabilities because we can't do log 0
        z = action_probs == 0.0
        z = z.float() * 1e-8
        log_action_probabilities = torch.log(action_probs + z)
        return action.detach().cpu(), action_probs, log_action_probabilities

  def get_det_action(self, state):
        action_probs = self.forward(state)
        dist = Categorical(action_probs)
        action = dist.sample().to(state.device)
        return action.detach().cpu()

class Critic(nn.Module):
  def __init__(self, state_size, action_size, hidden_size=32, seed=1):
        """Critic Network 
        Learns to build a critic (value) that maps state action pairs to Q values
        """        
        super(Critic, self).__init__()
        self.seed = torch.manual_seed(seed)
        self.fc1 = nn.Linear(8, hidden_size)
        self.fc2 = nn.Linear(hidden_size, hidden_size)
        self.fc3 = nn.Linear(hidden_size, action_size)
        self.reset_parameters()
        self.state_size = state_size

  def reset_parameters(self):
        self.fc1.weight.data.uniform_(*hidden_init(self.fc1))
        self.fc2.weight.data.uniform_(*hidden_init(self.fc2))
        self.fc3.weight.data.uniform_(-3e-3, 3e-3)

  def forward(self, state):
        d = len(state.shape)
        l1,_,l2 = torch.tensor_split(state, (self.state_size-7,-7), dim=d-1)
        l1 = torch.mean(torch.unsqueeze(l1,dim=d-1),dim=d)
        x = torch.cat((l1, l2), dim=d-1)
        x = F.relu(self.fc1(x))
        x = F.relu(self.fc2(x))
        return self.fc3(x)
    
class SAC(nn.Module):
    """Interacts with and learns from the environment."""
    
    def __init__(self,
                        gamma,
                        tau,
                        hidden_size,
                        learning_rate,
                        state_size,
                        action_size,
                        device
                ):
        """Initialize an Agent object.
        
        Params
        ======
            state_size (int): dimension of each state
            action_size (int): dimension of each action
            random_seed (int): random seed
        """
        super(SAC, self).__init__()
        self.state_size = state_size
        self.action_size = action_size

        self.device = device
        
        self.gamma = gamma
        self.tau = tau
        hidden_size = int(hidden_size)
        #learning_rate = 5e-4
        self.clip_grad_param = 1

        self.target_entropy = -action_size  # -dim(A)

        self.log_alpha = torch.tensor([0.0], requires_grad=True)
        self.alpha = self.log_alpha.exp().detach()
        self.alpha_optimizer = optim.Adam(params=[self.log_alpha], lr=learning_rate) 
                
        # Actor Network 

        self.actor_local = Actor(state_size, action_size, hidden_size).to(device)
        self.actor_optimizer = optim.Adam(self.actor_local.parameters(), lr=learning_rate)     
        
        # Critic Network (w/ Target Network)

        self.critic1 = Critic(state_size, action_size, hidden_size, 2).to(device)
        self.critic2 = Critic(state_size, action_size, hidden_size, 1).to(device)
        
        assert self.critic1.parameters() != self.critic2.parameters()
        
        self.critic1_target = Critic(state_size, action_size, hidden_size).to(device)
        self.critic1_target.load_state_dict(self.critic1.state_dict())

        self.critic2_target = Critic(state_size, action_size, hidden_size).to(device)
        self.critic2_target.load_state_dict(self.critic2.state_dict())

        self.critic1_optimizer = optim.Adam(self.critic1.parameters(), lr=learning_rate)
        self.critic2_optimizer = optim.Adam(self.critic2.parameters(), lr=learning_rate) 

    
    def get_action(self, state):
        """Returns actions for given state as per current policy."""
        state = torch.from_numpy(state).float().to(self.device)
        
        with torch.no_grad():
            action = self.actor_local.get_det_action(state)
        return action.numpy()

    def calc_policy_loss(self, states, alpha):
        _, action_probs, log_pis = self.actor_local.evaluate(states)

        q1 = self.critic1(states)   
        q2 = self.critic2(states)
        min_Q = torch.min(q1,q2)
        actor_loss = (action_probs * (alpha * log_pis - min_Q )).sum(1).mean()
        log_action_pi = torch.sum(log_pis * action_probs, dim=1)
        return actor_loss, log_action_pi
    
    def learn(self, experiences, gamma):
        states, actions, rewards, next_states, dones = experiences

        # ---------------------------- update actor ---------------------------- #
        current_alpha = copy.deepcopy(self.alpha)
        actor_loss, log_pis = self.calc_policy_loss(states, current_alpha.to(self.device))
        self.actor_optimizer.zero_grad()
        actor_loss.backward()
        self.actor_optimizer.step()
        
        # Compute alpha loss
        alpha_loss = - (self.log_alpha.exp() * (log_pis.cpu() + self.target_entropy).detach().cpu()).mean()
        self.alpha_optimizer.zero_grad()
        alpha_loss.backward()
        self.alpha_optimizer.step()
        self.alpha = self.log_alpha.exp().detach()

        # ---------------------------- update critic ---------------------------- #
        # Get predicted next-state actions and Q values from target models
        with torch.no_grad():
            _, action_probs, log_pis = self.actor_local.evaluate(next_states)
            Q_target1_next = self.critic1_target(next_states)
            Q_target2_next = self.critic2_target(next_states)
            Q_target_next = action_probs * (torch.min(Q_target1_next, Q_target2_next) - self.alpha.to(self.device) * log_pis)

            # Compute Q targets for current states (y_i)
            Q_targets = rewards + (gamma * (1 - dones) * Q_target_next.sum(dim=1).unsqueeze(-1)) 

        # Compute critic loss
        q1 = self.critic1(states).gather(1, actions.long())
        q2 = self.critic2(states).gather(1, actions.long())
        
        critic1_loss = 0.5 * F.mse_loss(q1, Q_targets)
        critic2_loss = 0.5 * F.mse_loss(q2, Q_targets)

        # Update critics
        # critic 1
        self.critic1_optimizer.zero_grad()
        critic1_loss.backward(retain_graph=True)
        clip_grad_norm_(self.critic1.parameters(), self.clip_grad_param)
        self.critic1_optimizer.step()
        # critic 2
        self.critic2_optimizer.zero_grad()
        critic2_loss.backward()
        clip_grad_norm_(self.critic2.parameters(), self.clip_grad_param)
        self.critic2_optimizer.step()

        # ----------------------- update target networks ----------------------- #
        self.soft_update(self.critic1, self.critic1_target)
        self.soft_update(self.critic2, self.critic2_target)
        
        return actor_loss.item(), alpha_loss.item(), critic1_loss.item(), critic2_loss.item(), current_alpha

    def soft_update(self, local_model , target_model):
        """Soft update model parameters.
        Args:
            local_model: PyTorch model (weights will be copied from)
            target_model: PyTorch model (weights will be copied to)
            tau (float): interpolation parameter 
        """
        for target_param, local_param in zip(target_model.parameters(), local_model.parameters()):
            target_param.data.copy_(self.tau*local_param.data + (1.0-self.tau)*target_param.data)
            


def save(args, save_name, model, wandb, ep=None):
    import os
    save_dir = './trained_models/' 
    if not os.path.exists(save_dir):
        os.makedirs(save_dir)
    if not ep == None:
        torch.save(model.state_dict(), save_dir + args.run_name + save_name + str(ep) + ".pth")
        wandb.save(save_dir + args.run_name + save_name + str(ep) + ".pth")
    else:
        torch.save(model.state_dict(), save_dir + args.run_name + save_name + ".pth")
        wandb.save(save_dir + args.run_name + save_name + ".pth")

def collect_random(env, dataset, num_samples=200):
    state = env.reset()
    for _ in range(num_samples):
        action = env.action_space.sample()
        next_state, reward, done, _ = env.step(action)
        dataset.add(state, action, reward, next_state, done)
        state = next_state
        if done:
            state = env.reset()
            

def get_config():
    parser = argparse.ArgumentParser(description='RL SAC')
    parser.add_argument("--run_name", type=str, default="SAC", help="Run name, default: SAC")
    parser.add_argument("--episodes", type=int, default=500, help="Number of episodes, default: 100")
    parser.add_argument("--buffer_size", type=int, default=100_000, help="Maximal training dataset size, default: 100_000")
    parser.add_argument("--seed", type=int, default=96, help="Seed, default: 1")
    parser.add_argument("--save_every", type=int, default=100, help="Saves the network every x epochs, default: 25")
    parser.add_argument("--batch_size", type=int, default=300, help="Batch size, default: 256")
    
    args = parser.parse_args("")
    return args


DAY0 = 50
DAYN = 60


def train(config, gamma, tau, hidden_size, learning_rate, **kwargs):
    np.random.seed(config.seed)
    random.seed(config.seed)
    torch.manual_seed(config.seed)
    env = MicroGridEnv(**kwargs)
    
    env.seedy(config.seed)
    env.action_space.seed(config.seed)

    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    
    steps = 10
    average10 = deque(maxlen=10)
    total_steps = 10
    wandb.init()
    with wandb.init(project="SAC microgrid", name=config.run_name, config=config):
        
        agent = SAC(gamma, tau, hidden_size, learning_rate, state_size=env.observation_space.shape[0],
                         action_size=env.action_space.n,
                         device=device)

        wandb.watch(agent, log="gradients", log_freq=10)

        buffer = ReplayBuffer(buffer_size=config.buffer_size, batch_size=config.batch_size, device=device)
        
        collect_random(env=env, dataset=buffer, num_samples=10000)
        
        total_reward = 0
        for day in range(DAY0, DAYN):
            for i in range(1, config.episodes+1):
                state = env.reset(day=day)
                episode_steps = 0
                rewards = 0
                while True:
                    action = agent.get_action(state)
                    steps += 1
                    next_state, reward, done, _ = env.step(action)
                    buffer.add(state, action, reward, next_state, done)
                    policy_loss, alpha_loss, bellmann_error1, bellmann_error2, current_alpha = agent.learn(buffer.sample(), gamma=0.99)
                    print("Alpha", current_alpha)
                    state = next_state
                    rewards += reward
                    episode_steps += 1
                    if done:
                        break
                
                average10.append(rewards)
                total_steps += episode_steps
                print("Episode: {} | Reward: {} | Polciy Loss: {} | Steps: {}".format(i, rewards, policy_loss, steps,))
                
                wandb.log({"Gamma": gamma,
                           "Tau": tau,
                           "Hidden size": int(hidden_size),
                           "Learning rate": learning_rate, 
                           "Reward": rewards,
                           "Total Reward": total_reward,
                           "Average10": np.mean(average10),
                           "Steps": total_steps,
                           "Policy Loss": policy_loss,
                           "Alpha Loss": alpha_loss,
                           "Bellmann error 1": bellmann_error1,
                           "Bellmann error 2": bellmann_error2,
                           "Alpha": current_alpha,
                           "Steps": steps,
                           "run_count": i,
                           "Buffer size": buffer.__len__()})

                if i % config.save_every == 0:
                    save(config, save_name="SAC", model=agent.actor_local, wandb=wandb, ep=0)
            final_reward = rewards
            total_reward += rewards
            wandb.log({"Total Reward": total_reward})
            wandb.log({"Final Reward": final_reward})
    print("Recompensa final: ", total_reward)
    return total_reward

# ============================================================================
# Self-adaptive
# ============================================================================

def train1(gamma, tau, hidden_size, learning_rate):
	""" Function to be optimized. """
    if __name__ == "__main__":
        config = get_config()
        r = train(config, gamma, tau, hidden_size, learning_rate)
        return r

n = 0

space = {
    "gamma": (0,1),
    "tau": (0,1),
    "hidden_size": (64,256),
    "learning_rate": (0,1)
}

acq = acquisition.ExpectedImprovement(0.5)

best = BayesianOptimization(
    f=train1,
    pbounds=space,
    verbose=2,
    acquisition_function=acq
)

logger = JSONLogger(path="./logs.json")
logger2 = JSONLogger(path="./logs2.json")
best.subscribe(Events.OPTIMIZATION_STEP, logger)
best.subscribe(Events.OPTIMIZATION_STEP, logger2)

best.maximize(
    init_points=0,
    n_iter=50,
)

print("Best Parameter Setting : {}".format(best.max["params"]))
print("Best Target Value      : {}".format(best.max["target"]))

results = best.res
results[-5:]

for i, res in enumerate(best.res):
    print("Iteration {}: /n/t{}".format(i, res))
print(best.max)


if __name__ == "__main__":
    config = get_config()
    train(config)
