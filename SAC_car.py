import os
import gym
import torch
import pprint
import argparse
import numpy as np
from torch.utils.tensorboard import SummaryWriter

from tianshou.env import DummyVectorEnv
from tianshou.utils.net.common import Net
from tianshou.trainer import offpolicy_trainer
from tianshou.data import Collector, ReplayBuffer
from SSAC import SSACPolicy
from tianshou.utils.net.continuous import Actor, ActorProb, Critic
from PriorGBM import PriorGBM
from ODENet import ODENet
from ODEGBM import ODEGBM
from NODAE import NODAE
from Plot_tensorboard import sort_file_by_time
import pdb


def get_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--task', type=str, default='CarRacing-v0')
    parser.add_argument('--seed', type=int, default=0)
    parser.add_argument('--buffer-size', type=int, default=20000)
    parser.add_argument('--actor-lr', type=float, default=3e-4)
    parser.add_argument('--critic-lr', type=float, default=1e-3)
    parser.add_argument('--il-lr', type=float, default=1e-3)
    parser.add_argument('--gamma', type=float, default=0.99)
    parser.add_argument('--tau', type=float, default=0.005)
    parser.add_argument('--alpha', type=float, default=0.2)
    parser.add_argument('--epoch', type=int, default=1)
    parser.add_argument('--step-per-epoch', type=int, default=1600)
    parser.add_argument('--collect-per-step', type=int, default=10)
    parser.add_argument('--batch-size', type=int, default=128)
    parser.add_argument('--layer-num', type=int, default=1)
    parser.add_argument('--training-num', type=int, default=8)
    parser.add_argument('--test-num', type=int, default=100)
    parser.add_argument('--logdir', type=str, default='log')
    parser.add_argument('--render', type=float, default=0.)
    parser.add_argument('--rew-norm', type=int, default=1)
    parser.add_argument('--ignore-done', type=int, default=1)
    parser.add_argument('--n-step', type=int, default=4)
    parser.add_argument('--train-simulator-step', type=int, default=3)
    parser.add_argument('--simulator-latent-dim', type=int, default=4)
    parser.add_argument('--simulator-hidden-dim', type=int, default=128)
    parser.add_argument('--simulator-lr', type=float, default=1e-3)
    parser.add_argument('--model', type=str, default='NODAE')
    parser.add_argument('--max-update-step', type=int, default=400)
    parser.add_argument('--simulator-batch-size', type=int, default=1024)
    parser.add_argument('--white-box', action='store_true', default=False)
    parser.add_argument('--loss-weight-trans', type=float, default=1)
    parser.add_argument('--loss-weight-ae', type=float, default=1)
    parser.add_argument('--loss-weight-rew', type=float, default=1)
    parser.add_argument('--noise-obs', type=float, default=0.0)
    parser.add_argument('--noise-rew', type=float, default=0.0)
    parser.add_argument('--n-simulator-step', type=int, default=200)
    parser.add_argument('--baseline', action='store_true', default=False)
    parser.add_argument(
        '--device', type=str,
        default='cuda' if torch.cuda.is_available() else 'cpu')
    args = parser.parse_known_args()[0]
    if args.baseline:
        args.train_simulator_step = 0
        args.max_update_step = 2 * args.epoch * args.step_per_epoch + 1
    return args


def test_sac(args=get_args()):
    torch.set_num_threads(1)  # we just need only one thread for NN
    env = gym.make(args.task)
    if args.task == 'Pendulum-v0':
        env.spec.reward_threshold = -250
    args.state_shape = env.observation_space.shape or env.observation_space.n
    args.action_shape = env.action_space.shape or env.action_space.n
    args.max_action = env.action_space.high[0]
    # you can also use tianshou.env.SubprocVectorEnv
    # train_envs = gym.make(args.task)
    train_envs = DummyVectorEnv(
        [lambda: gym.make(args.task) for _ in range(args.training_num)])
    # test_envs = gym.make(args.task)
    test_envs = DummyVectorEnv(
        [lambda: gym.make(args.task) for _ in range(args.test_num)])
    # seed
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    train_envs.seed(args.seed)
    test_envs.seed(args.seed)
    # model
    net = Net(args.layer_num, args.state_shape, device=args.device)
    actor = ActorProb(
        net, args.action_shape, args.max_action, args.device, unbounded=True
    ).to(args.device)
    actor_optim = torch.optim.Adam(actor.parameters(), lr=args.actor_lr)
    net_c1 = Net(args.layer_num, args.state_shape,
                 args.action_shape, concat=True, device=args.device)
    critic1 = Critic(net_c1, args.device).to(args.device)
    critic1_optim = torch.optim.Adam(critic1.parameters(), lr=args.critic_lr)
    net_c2 = Net(args.layer_num, args.state_shape,
                 args.action_shape, concat=True, device=args.device)
    critic2 = Critic(net_c2, args.device).to(args.device)
    critic2_optim = torch.optim.Adam(critic2.parameters(), lr=args.critic_lr)

    if args.model == 'ODEGBM':
        model = ODEGBM(args).to(args.device)
    elif args.model == 'PriorGBM':
        model = PriorGBM(args).to(args.device)
    elif args.model == 'NODAE':
        model = NODAE(args).to(args.device)
    else:
        assert args.model == 'ODENet'
        model = ODENet(args).to(args.device)

    policy = SSACPolicy(
        actor, actor_optim, critic1, critic1_optim, critic2, critic2_optim, model, args,
        action_range=[env.action_space.low[0], env.action_space.high[0]],
        tau=args.tau, gamma=args.gamma, alpha=args.alpha,
        reward_normalization=args.rew_norm,
        ignore_done=args.ignore_done,
        estimation_step=args.n_step)
    # collector
    train_collector = Collector(
        policy, train_envs, ReplayBuffer(args.buffer_size))
    test_collector = Collector(policy, test_envs)
    # train_collector.collect(n_step=args.buffer_size)
    # log
    log_path = os.path.join(args.logdir, args.task, 'sac')
    if args.baseline:
        if not os.path.exists(log_path + '/baseline/'):
            os.makedirs(log_path + '/baseline/')
        writer = SummaryWriter(log_path + '/baseline')
    else:
        writer = SummaryWriter(log_path)

    def save_fn(policy):
        # torch.save(policy.state_dict(), os.path.join(log_path, 'policy.pth'))
        pass

    def stop_fn(mean_rewards):
        # return mean_rewards >= env.spec.reward_threshold
        return False

    # trainer
    result = offpolicy_trainer(
        policy, train_collector, test_collector, args.epoch,
        args.step_per_epoch, args.collect_per_step, args.test_num,
        args.batch_size, stop_fn=stop_fn, save_fn=save_fn, writer=writer)
    # assert stop_fn(result['best_reward'])
    if __name__ == '__main__':
        pprint.pprint(result)
        # Let's watch its performance!
        env = gym.make(args.task)
        policy.eval()
        collector = Collector(policy, env)
        result = collector.collect(n_episode=1, render=args.render)
        print(f'Final reward: {result["rew"]}, length: {result["len"]}')


if __name__ == '__main__':
    test_sac()
