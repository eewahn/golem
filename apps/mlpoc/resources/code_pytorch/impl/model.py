import abc
import itertools
import os
from copy import deepcopy
from typing import Dict, Any

import dill as pickle
import numpy as np
from params import network_configuration
from torch import nn, torch, from_numpy
from torch.autograd import Variable

from .batchmanager import IrisBatchManager
from .box_callback import BlackBoxFileCallback
from .common_utils import name_of_model_dump
from .config import (BATCH_SIZE,
                     LEARNING_RATE,
                     NUM_CLASSES)
from .hash import PyTorchHash, StateHash
from .inside_utils import derandom
from .net import Net

network_configuration = dict(network_configuration)


class Model(metaclass=abc.ABCMeta):
    @abc.abstractmethod
    def run_one_batch(self, x: np.ndarray, y: np.ndarray) -> None:
        """
        Train model on one batch of training samples
        :param x: Training examples
        :param y: Answers
        :return: None
        """
        pass

    @property
    @abc.abstractmethod
    def kwargs(self) -> Dict[str, Any]:
        """
        kwargs are parameters used to construct the class
        Storing them makes it possible to dump the model - without a pickle
        just saving kwargs - and then reconstructing class
        :return: dict of kwargs passed to __init__
        """
        pass

    def get_hash(self) -> 'Hash':
        return self.get_model_hash(self)

    @abc.abstractmethod
    def get_model_hash(self) -> 'Hash':
        pass

    @property
    @abc.abstractmethod
    def net(self) -> torch.Module:
        pass


class IrisSimpleModel(Model):
    def __init__(self,
                 input_size: int,
                 hidden_size: int,
                 num_classes: int,
                 learning_rate: int):
        self._kwargs = {}
        self._kwargs["input_size"] = input_size
        self._kwargs["hidden_size"] = hidden_size
        self._kwargs["num_classes"] = num_classes
        self._kwargs["learning_rate"] = learning_rate

        self._net = Net(input_size=input_size,
                        hidden_size=hidden_size,
                        num_classes=num_classes)

        self.criterion = nn.CrossEntropyLoss()
        self.optimizer = torch.optim.SGD(self.net.parameters(),
                                         lr=learning_rate)

    def run_one_batch(self, x: np.ndarray, y: np.ndarray):
        derandom()
        x = Variable(from_numpy(x).view(BATCH_SIZE, -1).type(torch.FloatTensor))

        y = np.argmax(y, axis=1)
        y = Variable(from_numpy(y).view(BATCH_SIZE).type(torch.LongTensor))

        self.optimizer.zero_grad()
        outputs = self._net(x)
        loss = self.criterion(outputs, y)
        loss.backward()
        self.optimizer.step()

    @staticmethod
    def get_model_hash(model):
        return PyTorchHash(model.net)

    @property
    def kwargs(self):
        return self._kwargs

    @property
    def net(self):
        return self._net


class ComputationState(object):
    def __init__(self, start_model: Model,
                 end_model: Model):
        self.start_model = start_model
        self.end_model = end_model

    def get_start_end(self):
        return self.start_model, self.end_model

    def update_before(self, model):
        self.start_model = model

    def update_after(self, model):
        self.end_model = model

    # useful for testing non-determinism
    def add_perturbation(self, eps: float):
        derandom()  # TODO keep track of all derandomizations
        for model in [self.start_model, self.end_model]:
            for v in model.net.parameters():
                numpy_form = v.data.numpy()
                perturbation = np.random.rand(numpy_form) * eps
                np.add(numpy_form, perturbation, out=numpy_form)


class ModelSerializer():
    def __init__(self, model: IrisSimpleModel, shared_path: str,
                 save_model_as_dict):
        self.model = model
        self.save_model_as_dict = save_model_as_dict
        self.shared_path = shared_path

        if not os.path.exists(self.shared_path):
            os.makedirs(self.shared_path)

    def _get_current_model_hash(self):
        return self.model.get_model_hash(self.model)

    def _get_path_to_save(self, model: Model, epoch: int, ext):
        dir = str(epoch)
        dir = os.path.join(self.shared_path, dir)

        if not os.path.exists(dir):
            os.makedirs(dir)

        filename = name_of_model_dump(epoch=epoch,
                                      hash=str(model.get_hash()),
                                      ext=ext)
        return os.path.join(dir, filename)

    def save(self, epoch, state):
        for mdl, ext in zip(state.get_start_end(), ["begin", "end"]):
            filepath = self._get_path_to_save(mdl, epoch, ext=ext)
            if self.save_model_as_dict:
                state_dict = {
                    "epoch": epoch,
                    # "minibatch": self.minibatch_num,
                    # "arch": config.ARCH,
                    "network_state_dict": mdl.net.state_dict(),
                    "optimizer_state_dict": mdl.optimizer.state_dict(),
                    "model_kwargs": mdl.kwargs
                }
                torch.save(state_dict, filepath)
            else:
                with open(filepath, "w") as f:
                    pickle.dump(self.model, f)

    @staticmethod
    def load(path):
        checkpoint = torch.load(path)
        model_kwargs = checkpoint["model_kwargs"]
        model = IrisSimpleModel(**model_kwargs)
        model.net.load_state_dict(checkpoint['network_state_dict'])
        model.optimizer.load_state_dict(checkpoint['optimizer_state_dict'])

        return model


class HonestModelRunner(object):
    def __init__(self,
                 shared_path: str,
                 data_file: str,
                 save_model_as_dict=True,
                 number_of_epochs=network_configuration["NUM_EPOCHS"]):

        self.black_box = BlackBoxFileCallback()
        self.batch_manager = IrisBatchManager(data_file)

        self.model = IrisSimpleModel(self.batch_manager.get_input_size(),
                                     network_configuration["HIDDEN_SIZE"],
                                     NUM_CLASSES,
                                     LEARNING_RATE)

        self.serializer = ModelSerializer(self.model, shared_path,
                                          save_model_as_dict)
        self.state = ComputationState(self.model, self.model)
        self.num_epochs = number_of_epochs

    def run_full_training(self):

        steps = network_configuration["STEPS_PER_EPOCH"]

        for epoch in range(self.num_epochs):
            self.state.update_before(deepcopy(self.model))

            for i, (x, y) in enumerate(itertools.islice(self.batch_manager,
                                                        steps)):
                self.model.run_one_batch(x, y)

            self.state.update_after(deepcopy(self.model))

            self.call_box(epoch, self.state)

    def call_box(self, epoch: int, state: ComputationState):
        box_decision = self.black_box.decide(str(StateHash(state)),
                                             number_of_epoch=epoch)
        if box_decision:
            self.serializer.save(epoch, state)

# for tests
# class SkippingDishonestModelRunner(HonestModelRunner):
#     """
#     This modelRunner skips epoch with some fixed probability_of_cheating
#     """
#     def __init__(self, probability_of_cheating: float, *args, **kwargs):
#         super().__init__(*args, **kwargs)
#         self.probability_of_cheating = probability_of_cheating
#
#     def run_full_training(self):
#         for epoch in range(self.num_epochs):
#             self.state.update_before(deepcopy(self.model))
#
#             for i, (x, y) in enumerate(
#                     itertools.islice(self.batch_manager, network_configuration["STEPS_PER_EPOCH"])):
#                 # with some probability, we we'll skip a step of computation
#                 if np.random.rand() < self.probability_of_cheating:
#                     self.model.run_one_batch(x, y)
#                 else:
#                     pass
#
#             self.state.update_before(deepcopy(self.model))
#             self.call_box(epoch, self.state)


# class CyclicBufferDishonestModelRunner(HonestModelRunner):
#     """
#     This ModelRunner creates a cyclic buffer with some known lenght_of_cb
#     And then just loops over it
#     """
#     def __init__(self, lenght_of_cb: int, added_eps: float, *args, **kwargs):
#         super().__init__(*args, **kwargs)
#         self.lenght_of_cb = lenght_of_cb
#         self.added_eps = added_eps
#
#     def run_full_training(self):
#         buffer = []
#         buffer_cur_pos = 0
#
#         for epoch in range(self.num_epochs):
#             if len(buffer) == self.lenght_of_cb:
#                 self.call_box(epoch, buffer[buffer_cur_pos])
#                 buffer_cur_pos = (buffer_cur_pos + 1) % self.lenght_of_cb
#             else:
#                 self.state.update_before(
#                     deepcopy(self.model))  # deepcopy needed
#
#                 for i, (x, y) in enumerate(
#                         itertools.islice(self.batch_manager, STEPS_PER_EPOCH)):
#                     self.model.run_one_batch(x, y)
#
#                 self.state.update_after(deepcopy(self.model))
#
#                 self.state.add_perturbation(self.added_eps)  # deepcopy needed
#                 buffer.append(copy(self.state))  # normal copy suffices
#                 self.call_box(epoch, self.state)
