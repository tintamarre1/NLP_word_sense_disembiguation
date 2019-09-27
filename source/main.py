#!/usr/bin/env python
# coding: utf-8

import os
os.environ['CUDA_LAUNCH_BLOCKING'] = "1"
import numpy as np
import pandas as pd
pd.set_option('mode.chained_assignment', None) # remove pandas copy to slice warnings
from argparse import ArgumentParser

import torch
from pytorch_transformers import AdamW
from tensorboardX import SummaryWriter
# High(er) level api to pytorch
from ignite.engine import Engine, Events
from ignite.metrics import Accuracy, Loss, RunningAverage, Precision, Recall
from ignite.handlers import ModelCheckpoint, EarlyStopping
from ignite.contrib.handlers import ProgressBar
# Custom modules
from models.bert import BertForWSD
from dataloaders.data_format_utils import preprocess_model_inputs
from dataloaders.dataloaders import TrainValDataloader


# Def Main pytorch-ignite evaluation functions

class Ignite_Engines():
    """
    wraps around and Instantiates several pytorch ignite functions    
    """
    def __init__(self,_model, _optimizer,_criterion,_device):
        self.model = _model
        self.optimizer = _optimizer
        self.criterion = _criterion
        self.device = _device

    def get_process_function(self):
        def process_function(engine, batch):
            self.model.train()
            self.optimizer.zero_grad()
            batch = (tens.to(self.device) for tens in batch)
            b_tokens_tensor, b_sentence_tensor, b_target_token_tensor, y = batch
            y_pred = self.model(b_tokens_tensor, b_sentence_tensor, b_target_token_tensor)
            loss = self.criterion(y_pred, y)
            loss.backward()
            self.optimizer.step()
            return loss.item()
        return process_function

    def get_eval_function(self):
        def eval_function(engine, batch):
            self.model.eval()
            with torch.no_grad():
                batch = (tens.to(self.device) for tens in batch)
                b_tokens_tensor, b_sentence_tensor, b_target_token_tensor, y = batch
                y_pred = self.model(b_tokens_tensor, b_sentence_tensor, b_target_token_tensor)
                return y_pred, y
        return eval_function
        
    def get_subset_eval_function(self):
        eval_function = self.get_eval_function()

        def subset_eval_function(engine, batch):
            """ Function ot be run on validation subset during the training process """
            y_pred, y = eval_function(engine, batch)
            with torch.no_grad():
                loss = self.criterion(y_pred, y)
                return loss.item()
        return subset_eval_function

def score_function(engine):
    val_loss = engine.state.metrics['bce']
    return -val_loss

def create_summary_writer(model, data_loader, log_dir, device):
    """
    Generates a tensorboardX summary writer to log metrics
    """
    writer = SummaryWriter(logdir=log_dir)
    data_loader_iter = iter(data_loader)
    batch = next(data_loader_iter)
    batch = tuple(b.to(device) for b in batch)[:-1]
    try:
        writer.add_graph(model, batch)
    except Exception as e:
        print("Failed to save model graph: {}".format(e))
    return writer

def get_new_run_directory(_logpath):
    """ Given log path returns new run directory path """
    if os.path.exists(_logpath):
        _listdirs = [int(d.split('run')[1]) for d in os.listdir(_logpath) if  str.startswith(d,'run')]
        _listdirs.sort()
        num = 0
        if _listdirs:
            num = _listdirs[-1]
        _new_run_directory = os.path.join(_logpath,'run{}'.format(num+1))
    else:
        _new_run_directory = os.path.join(_logpath,'run0')
        os.makedirs(_new_run_directory)
    return _new_run_directory

def get_set_optimizer(_model,lr=2e-5,weight_decay_rate=0.1):
    #optimizer = torch.optim.Adam(model.parameters(), lr=1e-4, weight_decay=1e-3)
    param_optimizer = list(_model.named_parameters())
    no_decay = ['bias', 'gamma', 'beta']

    optimizer_grouped_parameters = [
        {'params': [p for n, p in param_optimizer if not any(nd in n for nd in no_decay)],
        'weight_decay_rate': weight_decay_rate},
        {'params': [p for n, p in param_optimizer if any(nd in n for nd in no_decay)],
        'weight_decay_rate': 0.0}]

    # To reproduce BertAdam specific behavior set correct_bias=False
    optimizer = AdamW(optimizer_grouped_parameters,
                    lr=lr)  
    return optimizer

def register_metrics(_criterion,
                     _trainer_engine,_subset_validation_engine,
                     _trainer_eval_engine,_validation_eval_engine):

    """
    given a set of engine class objects corresponding to the training and 
    validation processes, attaches relevant metrics to proper objects.
    The criterion is  the pytorch loss function
    """
    def thresholded_output_transform(output):
        y_pred, y = output
        y_pred = torch.round(y_pred)
        return y_pred, y

    RunningAverage(output_transform=lambda x: x).attach(_trainer_engine, 'loss')
    # Validation Accuracy (on subset of val regularly during epoch calculated on subset)
    RunningAverage(output_transform=lambda x: x).attach(_subset_validation_engine, 'loss')
    # Trainer Accuracy (on full dataset after epoch)
    Accuracy(output_transform=thresholded_output_transform).attach(_trainer_eval_engine, 'accuracy')
    Loss(_criterion).attach(_trainer_eval_engine, 'bce')
    # Validation Accuracy (on full val dataset after epoch)

    Accuracy(output_transform=thresholded_output_transform).attach(_validation_eval_engine, 'accuracy')
    Loss(_criterion).attach(_validation_eval_engine, 'bce')

    precision = Precision(output_transform=thresholded_output_transform,average=True)
    recall = Recall(output_transform=thresholded_output_transform,average=True)

    precision.attach(_validation_eval_engine, 'Precision')
    recall.attach(_validation_eval_engine, 'Recall')
    F1 = (precision * recall * 2 / (precision + recall))
    F1.attach(_validation_eval_engine, 'F1') 


def run(dtloader, epochs, lr,weight_decay_rate, log_interval=10, 
        log_dir='../data/logs',model_checkpoint_dir='../data/model_checkpoints/'):
    """
    given dataloader (of TrainValDataloader class) for train, sample_validation, 
    and vallidation sets up model, optimier, criterion, metrics and log handlers and runs model.
    """
   
    trainig_log_interval = log_interval
    subset_validation_log_interval = log_interval
        
    model = BertForWSD() 
        
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    optimizer = get_set_optimizer(model,lr=lr,weight_decay_rate=weight_decay_rate)
    criterion = torch.nn.CrossEntropyLoss()

    IE = Ignite_Engines(model,optimizer,criterion,device)
    #IE.get_process_function()
    
    trainer = Engine(IE.get_process_function())
    subset_validation_evaluator = Engine(IE.get_subset_eval_function()) # validation evluator used during training
    train_evaluator = Engine(IE.get_eval_function()) # Used after training epoch
    validation_evaluator = Engine(IE.get_eval_function()) # Used after training epoch
    
    
    register_metrics(criterion,trainer,subset_validation_evaluator,
                train_evaluator,validation_evaluator)

    if device.type == 'cuda':
        torch.cuda.empty_cache()
        model.to(device)

    if log_dir:
        _run_logdir = get_new_run_directory(log_dir)
        writer = create_summary_writer(model, dtloader.train_dataloader, _run_logdir, device)

    # Progress bar
    
    pbar = ProgressBar(persist=True, bar_format="")
    pbar.attach(trainer, ['loss'])   

    # register events

    @trainer.on(Events.ITERATION_COMPLETED)
    def log_training_loss(engine):
        iterations = (engine.state.iteration - 1) % len(dtloader.train_dataloader) + 1
        if iterations % trainig_log_interval == 0:
            #print("Epoch[{}] Iteration[{}/{}] Loss: {:.2f}"
            #      "".format(engine.state.epoch, iter, len(dl.train_dataloader), engine.state.output))
            
            # run validation evaluator every time we log 
            if log_dir:
                # Training evaluation metrics to be logged during training
                writer.add_scalar("running_metrics/train_loss", 
                                  engine.state.output, engine.state.iteration)
            
    def subset_validation_loss(engine):
        iterations = (engine.state.iteration - 1) % len(dtloader.train_dataloader) + 1
        if iterations % subset_validation_log_interval == 0:
            subset_validation_evaluator.run(dl.subset_val_dataloader)
            val_metrics = subset_validation_evaluator.state.metrics
            val_loss = val_metrics['loss']
            if log_dir:
                # Subset evaluation  metrics to be logged during training
                writer.add_scalar("running_metrics/val_loss", val_loss, engine.state.iteration)
            
    @trainer.on(Events.EPOCH_COMPLETED)
    def log_training_results(engine):
        train_evaluator.run(dtloader.train_dataloader)
        metrics = train_evaluator.state.metrics
        avg_accuracy = metrics['accuracy']
        avg_bce = metrics['bce']
        pbar.log_message(
            "Training Results - Epoch: {}  Avg accuracy: {:.2f} Avg loss: {:.2f}"
            .format(engine.state.epoch, avg_accuracy, avg_bce))
        if log_dir:
            writer.add_scalar("training/avg_loss", avg_accuracy, engine.state.epoch)
            writer.add_scalar("training/avg_accuracy", avg_bce, engine.state.epoch)
        
    def log_validation_results(engine):
        validation_evaluator.run(dtloader.val_dataloader)
        metrics = validation_evaluator.state.metrics
        avg_accuracy = metrics['accuracy']
        avg_bce = metrics['bce']
        avg_precision = metrics['Precision']
        avg_recall = metrics['Recall']
        avg_F1 = metrics['F1']
        pbar.log_message(
            "Validation Results - Epoch: {} Averages: Acc: {:.3f} Loss: {:.3f} Precision: {:.3f} Recall: {:.3f} F1: {:.3f}"
            .format(engine.state.epoch, avg_accuracy, avg_bce, avg_precision, avg_recall, avg_F1))
        pbar.n = pbar.last_print_n = 0
        if log_dir:
            writer.add_scalar("validation/avg_accuracy", avg_accuracy, engine.state.epoch)
            writer.add_scalar("validation/avg_loss", avg_bce, engine.state.epoch)
            writer.add_scalar("validation/avg_F1", avg_F1, engine.state.epoch)
            writer.add_scalar("validation/avg_precision", avg_precision, engine.state.epoch)
            writer.add_scalar("validation/avg_recall", avg_recall, engine.state.epoch)   
    
    # Events Handler (Sets when given events are happening)
    handler = EarlyStopping(patience=3, score_function=score_function, trainer=trainer)
    validation_evaluator.add_event_handler(Events.COMPLETED, handler)
    trainer.add_event_handler(Events.EPOCH_COMPLETED, log_validation_results)
    trainer.add_event_handler(Events.ITERATION_COMPLETED, subset_validation_loss)
    trainer.add_event_handler(Events.EPOCH_COMPLETED, log_validation_results)

    # Checkpoints model/Adds event handler to trainer engine
    if model_checkpoint_dir:
        model_checkpoint_path = get_new_run_directory(model_checkpoint_dir)
        checkpointer = ModelCheckpoint(model_checkpoint_path, 'bertWSD', save_interval=1, n_saved=2, 
                                    create_dir=True, save_as_state_dict=True,require_empty=False)
        trainer.add_event_handler(Events.EPOCH_COMPLETED, checkpointer, {'bertWSD': model})


    # kick everything off
    trainer.run(dtloader.train_dataloader, max_epochs=epochs)

    writer.close()


if __name__ == "__main__":


    parser = ArgumentParser()
    parser.add_argument('--batch_size', type=int, default=16,
                        help='input batch size for training (default: 64)')
    #parser.add_argument('--val_batch_size', type=int, default=1000,
    #                    help='input batch size for validation (default: 1000)')
    parser.add_argument('--epochs', type=int, default=10,
                       help='number of epochs to train (default: 10)')
    parser.add_argument('--lr', type=float, default=2e-5,
                        help='learning rate (default: 0.01)')
    parser.add_argument('--weight_decay', type=float, default=0.01,
                        help='SGD momentum (default: 0.5)')
    #parser.add_argument('--momentum', type=float, default=0.5,
    #                    help='SGD momentum (default: 0.5)')
    parser.add_argument('--log_interval', type=int, default=20,
                        help='how many batches to wait before logging training status')
    parser.add_argument("--log_dir", type=str, default="../data/logs",
                        help="log directory for Tensorboard log output")
    parser.add_argument("--checkpoint_dir", type=str, default="../data/model_checkpoints/",
                        help="log directory for Tensorboard log output")
    parser.add_argument("--n_files", type=int, default=2,
                        help="semcor number of files"),
    parser.add_argument("--data_path", type=str, default="../data/preprocessed/fullcorpus.feather",
                        help="Input data path")
    args = parser.parse_args()

    N_FILES = 5
           
    print('Running with {}'.format(args))
    print()
    # ## Process Data
    print('Formatting corpus')
    full_dataset = pd.read_feather(args.data_path)
    unique_files = full_dataset.file.unique()
    np.random.shuffle(unique_files)
    # take N files out of dataset
    reduced_dataset = full_dataset[full_dataset.file.isin(unique_files[:args.n_files])]
    #reduced_dataset = full_dataset.sample(100)
    df = preprocess_model_inputs(reduced_dataset,sample_size=None)
    dl = TrainValDataloader(df,args.batch_size,val_sample_dataloader=True)    
    print()
    print('Initiating training')
    run(dl, args.epochs, args.lr,args.weight_decay, log_interval=args.log_interval, 
        log_dir=args.log_dir,model_checkpoint_dir=args.checkpoint_dir)



    

