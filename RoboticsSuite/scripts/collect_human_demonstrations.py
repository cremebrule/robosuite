"""
A script to collect a batch of human demonstrations that can be used
to generate a learning curriculum (see `demo_learning_curriculum.py`).

The demonstrations can be played back using the `playback_demonstrations_from_pkl.py`
script.
"""

import os
import shutil
import time
import argparse
import datetime
import h5py
from glob import glob
import numpy as np

import RoboticsSuite
import RoboticsSuite.utils.transform_utils as T
from RoboticsSuite.wrappers import IKWrapper
from RoboticsSuite.wrappers import DataCollectionWrapper


def collect_human_trajectory(env, device):
    """
    Use the device (keyboard or SpaceNav 3D mouse) to collect a demonstration.
    The rollout trajectory is saved to files in npz format.
    Modify the DataCollectionWrapper wrapper to add new fields or change data formats.

    Args:
        env: environment to control
        device (instance of Device class): to receive controls from the device
    """

    obs = env.reset()

    # rotate the gripper so we can see it easily
    env.set_robot_joint_positions([0, -1.18, 0.00, 2.18, 0.00, 0.57, 1.5708])

    env.viewer.set_camera(camera_id=2)
    env.render()

    # episode terminates on a spacenav reset input or if task is completed
    reset = False
    device.start_control()
    while not (reset or env._check_success()):
        state = device.get_controller_state()
        dpos, rotation, grasp, reset = (
            state["dpos"],
            state["rotation"],
            state["grasp"],
            state["reset"],
        )

        # convert into a suitable end effector action for the environment
        current = env._right_hand_orn
        drotation = current.T.dot(rotation)  # relative rotation of desired from current
        dquat = T.mat2quat(drotation)
        grasp = grasp - 1.  # map 0 to -1 (open) and 1 to 0 (closed halfway)
        action = np.concatenate([dpos, dquat, [grasp]])

        obs, reward, done, info = env.step(action)
        env.render()

    # cleanup for end of data collection episodes
    env.close()

def gather_demonstrations_as_hdf5(directory, out_dir):
    """
    Gathers the demonstrations saved in @directory into a
    single hdf5 file, and another directory that contains the 
    raw model.xml files.

    The strucure of the hdf5 file is as follows.

    data (group)
        date (attribute) - date of collection
        time (attribute) - time of collection
        repository_version (attribute) - repository version used during collection
        env (attribute) - environment name on which demos were collected

        demo1 (group) - every demonstration has a group
            model_file (attribute) - name of corresponding model xml in `models` directory
            states (dataset) - flattened mujoco states
            joint_velocities (dataset) - joint velocities applied during demonstration
            gripper_actuations (dataset) - gripper controls applied during demonstration
            right_dpos (dataset) - end effector delta position command for
                single arm robot or right arm
            right_dquat (dataset) - end effector delta rotation command for
                single arm robot or right arm
            left_dpos (dataset) - end effector delta position command for
                left arm (bimanual robot only)
            left_dquat (dataset) - end effector delta rotation command for
                left arm (bimanual robot only)

        demo2 (group)
        ...

    Args:
        directory (str): Path to the directory containing raw demonstrations.
        out_dir (str): Path to where to store the hdf5 file and model xmls. 
            The model xmls will be stored in a subdirectory called `models`.
    """

    # store model xmls in this directory
    model_dir = os.path.join(out_dir, "models")
    if os.path.isdir(model_dir):
        shutil.rmtree(model_dir)
    os.makedirs(model_dir)

    hdf5_path = os.path.join(out_dir, "demo.hdf5")
    f = h5py.File(hdf5_path, "w")

    # store some metadata in the attributes of one group
    grp = f.create_group("data")

    num_eps = 0
    env_name = None # will get populated at some point

    for ep_directory in os.listdir(directory):

        state_paths = os.path.join(directory, ep_directory, "state_*.npz")
        states = []
        actions = []
        starts = []
        joint_velocities = []
        gripper_actuations = []
        right_dpos = []
        right_dquat = []
        left_dpos = []
        left_dquat = []

        for state_file in sorted(glob(state_paths)):
            dic = np.load(state_file)
            env_name = str(dic["env"])
            # Note how we index the actions here. This is because when the DataCollector wrapper
            # recorded the states and actions, the states were recorded AFTER playing that action.
            # for s, ai in zip(dic["states"][:-1], dic["action_infos"][1:]):
            for s, ai in zip(dic["states"], dic["action_infos"]):
                states.append(s)
                actions.append(ai["action"])
                starts.append(ai["start"])
                joint_velocities.append(ai["joint_velocities"])
                gripper_actuations.append(ai["gripper_actuation"])
                right_dpos.append(ai.get("right_dpos", []))
                right_dquat.append(ai.get("right_dquat", []))
                left_dpos.append(ai.get("left_dpos", []))
                left_dquat.append(ai.get("left_dquat", []))

        if len(states) == 0:
            continue

        num_eps += 1
        ep_data_grp = grp.create_group("demo_{}".format(num_eps))

        # store model file name as an attribute
        ep_data_grp.attrs["model_file"] = "model_{}.xml".format(num_eps)

        # write datasets for states and actions
        ep_data_grp.create_dataset("states", data=np.array(states))
        ep_data_grp.create_dataset("actions", data=np.array(actions))
        ep_data_grp.create_dataset("starts", data=np.array(starts))
        ep_data_grp.create_dataset("joint_velocities", data=np.array(joint_velocities))
        ep_data_grp.create_dataset("gripper_actuations", data=np.array(gripper_actuations))
        ep_data_grp.create_dataset("right_dpos", data=np.array(right_dpos))
        ep_data_grp.create_dataset("right_dquat", data=np.array(right_dquat))
        ep_data_grp.create_dataset("left_dpos", data=np.array(left_dpos))
        ep_data_grp.create_dataset("left_dquat", data=np.array(left_dquat))

        # copy over and rename model xml
        xml_path = os.path.join(directory, ep_directory, "model.xml")
        shutil.copy(xml_path, model_dir)
        os.rename(os.path.join(model_dir, "model.xml"), 
                  os.path.join(model_dir, "model_{}.xml".format(num_eps)))


    # write dataset attributes (metadata)
    now = datetime.datetime.now()
    grp.attrs["date"] = "{}-{}-{}".format(now.month, now.day, now.year)
    grp.attrs["time"] = "{}:{}:{}".format(now.hour, now.minute, now.second)
    grp.attrs["repository_version"] = RoboticsSuite.__version__
    grp.attrs["env"] = env_name

    f.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--directory",
        type=str,
        default=os.path.join(RoboticsSuite.models.assets_root, "demonstrations"),
    )
    parser.add_argument("--environment", type=str, default="SawyerLift")
    parser.add_argument("--device", type=str, default="keyboard")
    args = parser.parse_args()

    # create original environment
    env = RoboticsSuite.make(
        args.environment,
        ignore_done=True,
        use_camera_obs=False,
        has_renderer=True,
        control_freq=100,
        gripper_visualization=True,
    )

    # enable controlling the end effector directly instead of using joint velocities
    env = IKWrapper(env)

    # wrap the environment with data collection wrapper
    tmp_directory = "/tmp/{}".format(str(time.time()).replace(".", "_"))
    env = DataCollectionWrapper(env, tmp_directory)

    # initialize device
    if args.device == "keyboard":
        from RoboticsSuite.devices import Keyboard

        device = Keyboard()
        env.viewer.add_keypress_callback("any", device.on_press)
        env.viewer.add_keyup_callback("any", device.on_release)
        env.viewer.add_keyrepeat_callback("any", device.on_press)
    elif args.device == "spacemouse":
        from RoboticsSuite.devices import SpaceMouse

        device = SpaceMouse()
    else:
        raise Exception(
            "Invalid device choice: choose either 'keyboard' or 'spacemouse'."
        )

    # make a new timestamped directory
    t1, t2 = str(time.time()).split(".")
    new_dir = os.path.join(args.directory, "{}_{}".format(t1, t2))
    os.makedirs(new_dir)

    # collect demonstrations
    while True:
        collect_human_trajectory(env, device)
        gather_demonstrations_as_hdf5(tmp_directory, new_dir)
