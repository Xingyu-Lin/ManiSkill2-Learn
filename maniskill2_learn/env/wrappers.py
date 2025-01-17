from collections import deque
import cv2
import numpy as np

from gym import spaces
from gym.core import ObservationWrapper, Wrapper
from gym.spaces import Discrete

from maniskill2_learn.utils.data import DictArray, GDict, deepcopy, encode_np, is_num, to_array, SLICE_ALL, to_np
from maniskill2_learn.utils.meta import Registry, build_from_cfg

from .observation_process import pcd_uniform_downsample

WRAPPERS = Registry("wrappers of gym environments")


class ExtendedWrapper(Wrapper):
    def __getattr__(self, name):
        # gym standard do not support name with '_'
        return getattr(self.env, name)


class BufferAugmentedEnv(ExtendedWrapper):
    """
    For multi-process environments.
    Use a buffer to transfer data from sub-process to main process!
    """

    def __init__(self, env, buffers):
        super(BufferAugmentedEnv, self).__init__(env)
        self.reset_buffer = GDict(buffers[0])
        self.step_buffer = GDict(buffers[:4])
        if len(buffers) == 5:
            self.vis_img_buffer = GDict(buffers[4])

    def reset(self, *args, **kwargs):
        self.reset_buffer.assign_all(self.env.reset(*args, **kwargs))

    def step(self, *args, **kwargs):
        alls = self.env.step(*args, **kwargs)
        self.step_buffer.assign_all(alls)

    def render(self, *args, **kwargs):
        ret = self.env.render(*args, **kwargs)
        if ret is not None:
            assert self.vis_img_buffer is not None, "You need to provide vis_img_buffer!"
            self.vis_img_buffer.assign_all(ret)


class ExtendedEnv(ExtendedWrapper):
    """
    Extended api for all environments, which should be also supported by VectorEnv.

    Supported extra attributes:
    1. is_discrete, is_cost, reward_scale

    Function changes:
    1. step: reward multiplied by a scale, convert all f64 to to_f32
    2. reset: convert all f64 to to_f32

    Supported extra functions:
    2. step_random_actions
    3. step states_actions
    """

    def __init__(self, env, reward_scale, use_cost):
        super(ExtendedEnv, self).__init__(env)
        assert reward_scale > 0, "Reward scale should be positive!"
        self.is_discrete = isinstance(env.action_space, Discrete)
        self.is_cost = -1 if use_cost else 1
        self.reward_scale = reward_scale * self.is_cost

    def _process_action(self, action):
        if self.is_discrete:
            if is_num(action):
                action = int(action)
            else:
                assert action.size == 1, f"Dim of discrete action should be 1, but we get {len(action)}"
                action = int(action.reshape(-1)[0])
        return action

    def reset(self, *args, **kwargs):
        kwargs = dict(kwargs)
        obs = self.env.reset(*args, **kwargs)
        return GDict(obs).f64_to_f32(wrapper=False)

    def step(self, action, *args, **kwargs):
        action = self._process_action(action)
        obs, reward, done, info = self.env.step(action, *args, **kwargs)
        if isinstance(info, dict) and "TimeLimit.truncated" not in info:
            info["TimeLimit.truncated"] = False
        obs, info = GDict([obs, info]).f64_to_f32(wrapper=False)
        return obs, np.float32(reward * self.reward_scale), np.bool_(done), info

    # The following three functions are available for VectorEnv too!
    def step_random_actions(self, num):
        from .env_utils import true_done
        # print("-----------------------------------------------")
        ret = None
        # import os
        # print(os.getpid(), obs)
        obs = GDict(self.reset()).copy(wrapper=False)
        
        # print(os.getpid(), self.env.level, obs)
        # print("-----------------------------------------------")
        # exit(0)

        for i in range(num):
            action = self.action_space.sample()
            next_obs, rewards, dones, infos = self.step(action)
            next_obs = GDict(next_obs).copy(wrapper=False)

            info_i = dict(
                obs=obs,
                next_obs=next_obs,
                actions=action,
                rewards=rewards,
                dones=true_done(dones, infos),
                infos=GDict(infos).copy(wrapper=False),
                episode_dones=dones,
            )
            info_i = GDict(info_i).to_array(wrapper=False)
            obs = GDict(next_obs).copy(wrapper=False)

            if ret is None:
                ret = DictArray(info_i, capacity=num)
            ret.assign(i, info_i)
            if dones:
                obs = GDict(self.reset()).copy(wrapper=False)
        return ret.to_two_dims(wrapper=False)

    def step_states_actions(self, states=None, actions=None):
        """
        For CEM only
        states: [N, NS]
        actions: [N, L, NA]
        return [N, L, 1]
        """
        assert actions.ndim == 3
        rewards = np.zeros_like(actions[..., :1], dtype=np.float32)
        for i in range(len(actions)):
            if hasattr(self, "set_state") and states is not None:
                self.set_state(states[i])
            for j in range(len(actions[i])):
                rewards[i, j] = self.step(actions[i, j])[1]
        return rewards

    def get_env_state(self):
        ret = {}
        if hasattr(self.env, "get_state"):
            ret["env_states"] = self.env.get_state()
        # if hasattr(self.env.unwrapped, "_scene") and save_scene_state:
        #     ret["env_scene_states"] = self.env.unwrapped._scene.pack()
        if hasattr(self.env, "level"):
            ret["env_levels"] = self.env.level
        return ret


@WRAPPERS.register_module()
class FixedInitWrapper(ExtendedWrapper):
    def __init__(self, env, init_state, level=None, *args, **kwargs):
        super(FixedInitWrapper, self).__init__(env)
        self.init_state = np.array(init_state)
        self.level = level

    def reset(self, *args, **kwargs):
        if self.level is not None:
            # For ManiSkill
            self.env.reset(level=self.level)
        else:
            self.env.reset()
        self.set_state(self.init_state)
        return self.env.get_obs()


class ManiSkill2_ObsWrapper(ExtendedWrapper, ObservationWrapper):
    def __init__(self, env, img_size=None, n_points=1200, n_goal_points=-1, obs_frame='base', ignore_dones=False, fix_seed=None):
        super().__init__(env)
        self.obs_frame = obs_frame
        if self.obs_mode == "state":
            pass
        elif self.obs_mode == "rgbd":
            self.img_size = img_size
        elif self.obs_mode == "pointcloud":
            self.n_points = n_points
            self.n_goal_points = n_goal_points
        elif self.obs_mode == 'particles':
            obs_space = env.observation_space            

        self.ignore_dones = ignore_dones

        self.fix_seed = fix_seed
    
    def reset(self, **kwargs):
        if self.fix_seed is not None:
            obs = self.env.reset(seed=self.fix_seed, **kwargs)
        else:
            obs = self.env.reset(**kwargs)
        return self.observation(obs)

    def step(self, action):
        next_obs, reward, done, info = super(ManiSkill2_ObsWrapper, self).step(action)        
        if self.ignore_dones:
            done = False
        return next_obs, reward, done, info

    def get_obs(self):   
        return self.observation(self.env.get_obs())

    def observation(self, observation):
        from mani_skill2.utils.common import flatten_state_dict
        from maniskill2_learn.utils.lib3d.mani_skill2_contrib import apply_pose_to_points, apply_pose_to_point
        from mani_skill2.utils.sapien_utils import vectorize_pose
        from sapien.core import Pose
        # print(GDict(observation).shape)
        # exit(0)

        # Note that rgb information returned from the environment must have range [0, 255]

        if self.obs_mode == "state":
            return observation
        elif self.obs_mode == "rgbd":
            """
            Example observation keys and respective shapes ('extra' keys don't necessarily match):
            {'image': 
                {'hand_camera': 
                    {'rgb': (128, 128, 3), 'depth': (128, 128, 1), 'camera_intrinsic': (3, 3), 
                    'camera_extrinsic': (4, 4), 'camera_extrinsic_base_frame': (4, 4)
                    }, 
                 'base_camera': 
                    {'rgb': (128, 128, 3), 'depth': (128, 128, 1), 'camera_intrinsic': (3, 3), 
                    'camera_extrinsic': (4, 4)
                    }
                }, 
             'agent': 
                {'qpos': 9, 'qvel': 9, 'controller': {'arm': {}, 'gripper': {}}, 'base_pose': 7}, 
             'extra': 
                {'tcp_pose': 7, 'goal_pos': 3}}            
            """

            obs = observation
            rgb, depth = [], []
            imgs = obs["image"]
            for cam_name in imgs:
                rgb.append(imgs[cam_name]["rgb"]) # each [H, W, 3]
                depth.append(imgs[cam_name]["depth"]) # each [H, W, 1]
            rgb = np.concatenate(rgb, axis=2)
            assert rgb.dtype == np.uint8
            depth = np.concatenate(depth, axis=2)
            obs.pop("image")
            
            if 'tcp_pose' in obs['extra'].keys() and 'goal_pos' in obs['extra'].keys():
                obs['extra']['tcp_to_goal_pos'] = (
                    obs['extra']['goal_pos'] - obs['extra']['tcp_pose'][:3]
                )
            s = flatten_state_dict(obs)

            if self.img_size is not None and self.img_size != (rgb.shape[0], rgb.shape[1]):
                rgb = cv2.resize(rgb.astype(np.float16), self.img_size, interpolation=cv2.INTER_LINEAR)
                depth = cv2.resize(depth, self.img_size, interpolation=cv2.INTER_LINEAR)
                rgb = rgb.astype(np.uint8)

            depth = depth.astype(np.float16)
            
            out_dict = {
                "rgb": rgb.transpose(2,0,1),
                "depth": depth.transpose(2,0,1),
                "state": s,
            }
            return out_dict
        elif self.obs_mode == "pointcloud":
            """
            Example observation keys and respective shapes ('extra' keys don't necessarily match):
            {'pointcloud': 
                {'xyz': (32768, 3), 'rgb': (32768, 3)}, 
                # 'xyz' can also be 'xyzw' with shape (N, 4), 
                # where the last dim indicates whether the point is inside the camera depth range
             'agent': 
                {'qpos': 9, 'qvel': 9, 'controller': {'arm': {}, 'gripper': {}}, 'base_pose': 7}, 
             'extra': 
                {'tcp_pose': 7, 'goal_pos': 3}
            }
            """

            if self.obs_frame in ['base', 'world']:
                base_pose = observation['agent']['base_pose']
                p, q = base_pose[:3], base_pose[3:]
                to_origin = Pose(p=p, q=q).inv()
            elif self.obs_frame == 'ee':
                pose = observation['extra']['tcp_pose']
                p, q = pose[:3], pose[3:]
                to_origin = Pose(p=p, q=q).inv()
            else:
                print('Unknown Frame', self.obs_frame)
                exit(0)

            pointcloud = observation['pointcloud'].copy()
            xyzw = pointcloud.pop('xyzw', None)
            if xyzw is not None:
                assert 'xyz' not in pointcloud.keys()
                mask = xyzw[:, -1] > 0.5
                xyz = xyzw[:, :-1] 
                for k in pointcloud.keys():
                    pointcloud[k] = pointcloud[k][mask]
                pointcloud['xyz'] = xyz[mask] 
            ret = {mode: pointcloud[mode] for mode in ['xyz', 'rgb', 'robot_seg'] if mode in pointcloud}

            ret['rgb'] = ret['rgb'] / 255.0
            if "PointCloudPreprocessObsWrapper" not in self.env.__str__():
                pcd_uniform_downsample(ret, env=self.env, ground_eps=1e-4, num=self.n_points)
            ret['xyz'] = apply_pose_to_points(ret['xyz'], to_origin)

            obs_extra_keys = observation['extra'].keys()
            tcp_pose = None
            if 'tcp_pose' in obs_extra_keys:
                tcp_pose = observation['extra']['tcp_pose']
                tcp_pose = Pose(p=tcp_pose[:3], q=tcp_pose[3:])
            goal_pos = None
            goal_pose = None
            if 'goal_pos' in obs_extra_keys:
                goal_pos = observation['extra']['goal_pos']
            elif 'goal_pose' in obs_extra_keys:
                goal_pos = observation['extra']['goal_pose'][:3]   
                goal_pose = observation['extra']['goal_pose']
                goal_pose = Pose(p=goal_pose[:3], q=goal_pose[3:])
            tcp_to_goal_pos = None
            if tcp_pose is not None and goal_pos is not None:
                tcp_to_goal_pos = goal_pos - observation['extra']['tcp_pose'][:3]

            if self.n_goal_points > 0:
                assert goal_pos is not None, (
                    "n_goal_points should only be used if goal_pos(e) is contained in the environment observation"
                )
                goal_pts_xyz = np.random.uniform(low=-1.0, high=1.0, size=(self.n_goal_points, 3)) * 0.01
                goal_pts_xyz = goal_pts_xyz + goal_pos[None, :]
                goal_pts_xyz = apply_pose_to_points(goal_pts_xyz, to_origin)
                goal_pts_rgb = np.zeros_like(goal_pts_xyz)
                goal_pts_rgb[:, 1] = 1
                ret['xyz'] = np.concatenate([ret['xyz'], goal_pts_xyz])
                ret['rgb'] = np.concatenate([ret['rgb'], goal_pts_rgb])            
                
            frame_related_states = []
            base_info = apply_pose_to_point(observation['agent']['base_pose'][:3], to_origin)
            frame_related_states.append(base_info)                
            if tcp_pose is not None:
                tcp_info = apply_pose_to_point(tcp_pose.p, to_origin)
                frame_related_states.append(tcp_info)          
            if goal_pos is not None:
                goal_info = apply_pose_to_point(goal_pos, to_origin)
                frame_related_states.append(goal_info)
            if tcp_to_goal_pos is not None:
                tcp_to_goal_info = apply_pose_to_point(tcp_to_goal_pos, to_origin)
                frame_related_states.append(tcp_to_goal_info)
            if 'gripper_pose' in obs_extra_keys:
                gripper_info = observation['extra']['gripper_pose'][:3]
                gripper_info = apply_pose_to_point(gripper_info, to_origin)
                frame_related_states.append(gripper_info)
            if 'joint_axis' in obs_extra_keys: # for TurnFaucet
                joint_axis_info = (
                    to_origin.to_transformation_matrix()[:3, :3] @ observation['extra']['joint_axis']
                )
                frame_related_states.append(joint_axis_info)          
            if 'link_pos' in obs_extra_keys: # for TurnFaucet
                link_pos_info = apply_pose_to_point(observation['extra']['link_pos'], to_origin)
                frame_related_states.append(link_pos_info)   
            frame_related_states = np.stack(frame_related_states, axis=0)
            ret['frame_related_states'] = frame_related_states

            frame_goal_related_poses = [] # 6D poses related to the goal wrt to self.obs_frame
            if goal_pose is not None:
                pose_wrt_origin = to_origin * goal_pose
                frame_goal_related_poses.append(
                    np.hstack([pose_wrt_origin.p, pose_wrt_origin.q])
                )
                if tcp_pose is not None:
                    pose_wrt_origin = goal_pose * tcp_pose.inv() # T_{tcp->goal}^{world}
                    pose_wrt_origin = to_origin * pose_wrt_origin
                    frame_goal_related_poses.append(
                        np.hstack([pose_wrt_origin.p, pose_wrt_origin.q])
                    )
            if len(frame_goal_related_poses) > 0:
                frame_goal_related_poses = np.stack(frame_goal_related_poses, axis=0)
                ret['frame_goal_related_poses'] = frame_goal_related_poses

            ret['to_frames'] = []
            base_pose = observation['agent']['base_pose']
            base_pose_p, base_pose_q = base_pose[:3], base_pose[3:]
            base_frame = (to_origin * Pose(p=base_pose_p, q=base_pose_q)).inv().to_transformation_matrix()
            ret['to_frames'].append(base_frame)

            if tcp_pose is not None:
                hand_frame = (to_origin * tcp_pose).inv().to_transformation_matrix()
                ret['to_frames'].append(hand_frame)
            if goal_pose is not None:
                goal_frame = (to_origin * goal_pose).inv().to_transformation_matrix()
                ret['to_frames'].append(goal_frame)
            ret['to_frames'] = np.stack(ret['to_frames'], axis=0) # [Nframe, 4, 4]

            agent_state = np.concatenate([observation['agent']['qpos'], observation['agent']['qvel']])
            if len(frame_related_states) > 0:
                agent_state = np.concatenate([agent_state, frame_related_states.flatten()])
            if len(frame_goal_related_poses) > 0:
                agent_state = np.concatenate([agent_state, frame_goal_related_poses.flatten()])
            for k in obs_extra_keys:
                if k not in ['tcp_pose', 'goal_pos', 'goal_pose', 
                             'tcp_to_goal_pos', 'tcp_to_goal_pose',
                             'joint_axis', 'link_pos']:
                    val = observation['extra'][k]
                    agent_state = np.concatenate(
                        [agent_state, 
                         val.flatten() if isinstance(val, np.ndarray) else np.array([val])]
                    )

            ret['state'] = agent_state
            return ret
        elif self.obs_mode == 'particles' and 'particles' in observation.keys():
            obs = observation
            xyz = obs['particles']['x']
            vel = obs['particles']['v']
            state = flatten_state_dict(obs['agent'])
            ret = {
                'xyz': xyz,
                'state': state,
            }
            return ret           
        else:
            return observation

    @property
    def _max_episode_steps(self):
        return self.env.unwrapped._max_episode_steps

    def render(self, mode="human", *args, **kwargs):
        if mode == "human":
            self.env.render(mode, *args, **kwargs)
            return

        if mode in ["rgb_array", "color_image"]:
            img = self.env.render(mode="rgb_array", *args, **kwargs)
        else:
            img = self.env.render(mode=mode, *args, **kwargs)
        if isinstance(img, dict):
            if "world" in img:
                img = img["world"]
            elif "main" in img:
                img = img["main"]
            else:
                print(img.keys())
                exit(0)
        if isinstance(img, dict):
            img = img["rgb"]
        if img.ndim == 4:
            assert img.shape[0] == 1
            img = img[0]
        if img.dtype in [np.float32, np.float64]:
            img = np.clip(img, a_min=0, a_max=1) * 255
        img = img[..., :3]
        img = img.astype(np.uint8)
        return img


class RenderInfoWrapper(ExtendedWrapper):
    def step(self, action):
        obs, rew, done, info = super().step(action)
        info['reward'] = rew
        self._info_for_render = info
        return obs, rew, done, info

    def reset(self, **kwargs):
        obs = super().reset(**kwargs)
        # self._info_for_render = self.env.get_info()
        self._info_for_render = {}
        return obs

    def render(self, mode, **kwargs):
        from maniskill2_learn.utils.image.misc import put_info_on_image

        if mode == "rgb_array" or mode == "cameras":
            img = super().render(mode=mode, **kwargs)
            return put_info_on_image(img, self._info_for_render, extras=None, overlay=True)
        else:
            return super().render(mode=mode, **kwargs)




def build_wrapper(cfg, default_args=None):
    return build_from_cfg(cfg, WRAPPERS, default_args)
