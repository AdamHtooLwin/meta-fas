import argparse
import random
import os
import yaml
import datetime
import glob
import pickle
import math

import pandas as pd
import numpy as np
import torchvision
import torch
from torch import nn, optim
from torch.utils.tensorboard import SummaryWriter

from datasets.datasets import Dataset, get_train_augmentations, get_test_augmentations
from models.scan import SCAN, ResNet18Classifier

import learn2learn as l2l

os.environ['http_proxy'] = 'http://192.41.170.23:3128'
os.environ['https_proxy'] = 'http://192.41.170.23:3128'


def get_latest_version(root_dir: str):
    last_version = 1
    directories = glob.glob(root_dir + "/version_*/")
    for directory in directories:
        version = int(directory.split("/")[-2].split("_")[-1])
        if version >= last_version:
            last_version = version + 1

    return last_version


def construct_grid(batch, nrow: int = 8):
    images = torchvision.utils.make_grid(batch, nrow=nrow)
    images = images.detach().cpu().numpy()
    return images


def accuracy(predictions, targets):
    predictions = predictions.argmax(dim=1)
    acc = (predictions == targets).sum().float()
    acc /= len(targets)
    return acc.item()


def main(configs, writer, lr=0.005, maml_lr=0.01, iterations=1000, ways=5, shots=1, tps=32, fas=5, device=torch.device("cpu"),
         download_location='~/data'):

    mean = (configs['mean']['r'], configs['mean']['g'], configs['mean']['b'])
    std = (configs['std']['r'], configs['std']['g'], configs['std']['b'])

    transforms = get_train_augmentations(configs['image_size'], mean=mean, std=std)

    print("Reading CSV...")
    df = pd.read_csv(configs['train_df'])

    train_dataset = Dataset(
        df, configs['dataset_root'], transforms, face_detector=None,
        bookkeeping_path=configs['bookkeeping_path'],
    )

    print("Generating metadataset using ", train_dataset.bookkeeping_path)
    meta_train = l2l.data.MetaDataset(train_dataset)

    print("Generating tasks...")
    train_tasks = l2l.data.TaskDataset(meta_train,
                                       task_transforms=[
                                            l2l.data.transforms.NWays(meta_train, ways),
                                            l2l.data.transforms.KShots(meta_train, shots + 5, replacement=True),
                                            l2l.data.transforms.LoadData(meta_train),
                                            l2l.data.transforms.RemapLabels(meta_train),
                                            l2l.data.transforms.ConsecutiveLabels(meta_train),
                                       ],
                                       num_tasks=10000)

    model = ResNet18Classifier(pretrained=False)
    model.to(device)
    meta_model = l2l.algorithms.MAML(model, lr=maml_lr)
    opt = optim.Adam(meta_model.parameters(), lr=lr)
    loss_func = nn.CrossEntropyLoss()

    print("Starting meta-training...")
    for iteration in range(iterations):
        iteration_error = 0.0
        iteration_acc = 0.0
        for _ in range(tps):
            learner = meta_model.clone()
            train_task = train_tasks.sample()
            data, labels = train_task
            data = data.to(device)
            labels = labels.to(device)

            # Separate data into adaptation/evalutation sets
            adaptation_indices = np.zeros(data.size(0), dtype=bool)
            adaptation_indices[:shots] = True
            length = adaptation_indices.shape[0]
            adaptation_indices[math.floor(length/2):math.floor(length/2 + shots)] = True

            # adaptation_indices[np.arange(shots*ways) * 2] = True
            evaluation_indices = torch.from_numpy(~adaptation_indices)
            adaptation_indices = torch.from_numpy(adaptation_indices)
            adaptation_data, adaptation_labels = data[adaptation_indices], labels[adaptation_indices]
            evaluation_data, evaluation_labels = data[evaluation_indices], labels[evaluation_indices]

            # Fast Adaptation
            for step in range(fas):
                train_error = loss_func(learner(adaptation_data), adaptation_labels)
                learner.adapt(train_error)

            # Compute validation loss
            predictions = learner(evaluation_data)
            valid_error = loss_func(predictions, evaluation_labels)
            valid_error /= len(evaluation_data)
            valid_accuracy = accuracy(predictions, evaluation_labels)
            iteration_error += valid_error
            iteration_acc += valid_accuracy

        iteration_error /= tps
        iteration_acc /= tps

        writer.add_scalar('Loss (iteration)', iteration_error, iteration)
        writer.add_scalar('Accuracy', iteration_acc, iteration)

        print('Iteration: {:d} Loss : {:.3f} Acc : {:.3f}'.format(iteration, iteration_error.item(), iteration_acc))

        # Take the meta-learning step
        opt.zero_grad()
        iteration_error.backward()
        opt.step()

        if iteration % configs['save_weight_interval'] == 0:
            torch.save(meta_model.state_dict(), weights_directory + "epoch_" + str(iteration) + ".pth")
            adaptation_images = construct_grid(adaptation_data, nrow=2*shots)
            evaluation_images = construct_grid(evaluation_data, nrow=10)

            writer.add_image("Last task (adapt)", adaptation_images, iteration)
            writer.add_image("Last task (evaluation)", evaluation_images, iteration)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Oulu Training')

    parser.add_argument("-c", "--config", required=True, help="Config file path.")
    parser.add_argument("-d", "--debug", required=False, type=bool, help="Checkpoint file path.", default=False)

    args = parser.parse_args()

    with open(args.config, 'r') as stream:
        configs = yaml.safe_load(stream)

    root_dir = os.getcwd()
    log_dir = configs['log_dir']
    log_dir = os.path.join(root_dir, log_dir)

    version = get_latest_version(log_dir)

    version_directory = log_dir + "version_" + str(version)
    if not os.path.isdir(version_directory):
        os.makedirs(version_directory)

    weights_directory = version_directory + "/weights/"
    if not os.path.isdir(weights_directory):
        os.makedirs(weights_directory)

    debug = args.debug

    start = datetime.datetime.now()
    configs['start'] = start
    configs['version'] = version
    configs['debug'] = debug
    configs['weights_directory'] = weights_directory

    with open(version_directory + '/configs.yml', 'w') as outfile:
        yaml.dump(configs, outfile, default_flow_style=False)

    # ========================= End of DevOps ==========================
    # ========================= Start of ML ==========================

    use_cuda = not configs['no_cuda'] and torch.cuda.is_available()

    random.seed(configs['seed'])
    np.random.seed(configs['seed'])
    torch.manual_seed(configs['seed'])
    if use_cuda:
        torch.cuda.manual_seed(configs['seed'])
        torch.backends.cudnn.deterministic = True
        torch.backends.cudnn.benchmark = False

    device = torch.device("cuda:" + str(configs['gpu']) if use_cuda else "cpu")

    print("Using", device)
    print("Version: ", version)
    print("Debug: ", debug)

    writer = SummaryWriter(log_dir=version_directory)

    main(configs=configs,
         writer=writer,
         lr=configs['lr'],
         maml_lr=configs['maml_lr'],
         iterations=configs['iterations'],
         ways=configs['ways'],
         shots=configs['shots'],
         tps=configs['tasks_per_step'],
         fas=configs['fast_adaption_steps'],
         device=device)
