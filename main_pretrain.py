# Original work Copyright (c) Meta Platforms, Inc. and affiliates. <https://github.com/facebookresearch/mae>
# Modified work Copyright 2024 ST-MEM paper authors. <https://github.com/bakqui/ST-MEM>

# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.
# --------------------------------------------------------
# References:
# DeiT: https://github.com/facebookresearch/deit
# BEiT: https://github.com/microsoft/unilm/tree/master/beit
# MAE: https://github.com/facebookresearch/mae
# --------------------------------------------------------

import argparse
import datetime
import json
import os
import time

import numpy as np
import torch
import torch.backends.cudnn as cudnn
import yaml
from torch.utils.tensorboard import SummaryWriter

import models
import util.misc as misc
from engine_pretrain import train_one_epoch
from util.dataset import build_dataset, get_dataloader
from util.misc import NativeScalerWithGradNormCount as NativeScaler
from util.optimizer import get_optimizer_from_config


def parse() -> dict:
    parser = argparse.ArgumentParser('ECG self-supervised pre-training')

    parser.add_argument('--config_path',
                        default='./configs/pretrain/st_mem_vit_beta_base_12lead.yaml',
                        type=str,
                        metavar='FILE',
                        help='YAML config file path')
    parser.add_argument('--output_dir',
                        default="",
                        type=str,
                        metavar='DIR',
                        help='path where to save')
    parser.add_argument('--exp_name',
                        default="",
                        type=str,
                        help='experiment name')
    parser.add_argument('--resume',
                        default="",
                        type=str,
                        metavar='PATH',
                        help='resume from checkpoint')
    parser.add_argument('--start_epoch',
                        default=0,
                        type=int,
                        metavar='N',
                        help='start epoch')

    args = parser.parse_args()
    with open(os.path.realpath(args.config_path), 'r') as f:
        config = yaml.load(f, Loader=yaml.FullLoader)
    for k, v in vars(args).items():
        if v:
            config[k] = v

    return config


def main(config):
    """
    모델 사전 학습을 위한 메인 함수.

    이 함수는 분산 학습 모드를 초기화하고, 데이터셋과 데이터로더를 설정하며, 
    모델을 정의하고, 옵티마이저를 구성하고, 학습 루프를 처리합니다. 또한 
    로깅, 체크포인트 저장 및 학습 완료 후 인코더 추출을 관리합니다.

    Args:
        config (dict): 학습에 필요한 모든 매개변수를 포함하는 설정 딕셔너리. 
                       분산 학습, 데이터셋, 데이터로더, 모델, 옵티마이저 및 
                       학습 하이퍼파라미터 설정을 포함합니다.

    Raises:
        ValueError: 설정에서 지정된 모델 이름이 지원되지 않는 경우 발생합니다.

    설명:
        - 분산 학습 모드가 활성화된 경우 초기화합니다.
        - 학습 데이터셋과 데이터로더를 설정합니다.
        - 설정에 따라 모델을 정의하고 지정된 장치로 이동합니다.
        - 유효 배치 크기를 기반으로 옵티마이저와 학습률을 구성합니다.
        - 학습 루프를 처리하며, 주기적으로 로깅 및 체크포인트를 저장합니다.
        - 학습 완료 후 인코더를 추출하고 저장합니다.
    """
    
    # 분산 학습 모드 초기화
    misc.init_distributed_mode(config['ddp'])

    # 현재 작업 디렉토리 출력
    print(f'job dir: {os.path.dirname(os.path.realpath(__file__))}')
    # 설정 파일 내용을 출력
    print(yaml.dump(config, default_flow_style=False, sort_keys=False))

    # 학습에 사용할 장치를 설정
    device = torch.device(config['device'])

    # 재현성을 위해 시드 고정
    seed = config['seed'] + misc.get_rank()
    torch.manual_seed(seed)
    np.random.seed(seed)

    # cuDNN 벤치마크 모드 활성화
    cudnn.benchmark = True

    # ECG 데이터셋 생성
    dataset_train = build_dataset(config['dataset'], split='train')
    # 데이터로더 생성
    data_loader_train = get_dataloader(dataset_train,
                                       is_distributed=config['ddp']['distributed'],
                                       mode='train',
                                       **config['dataloader'])

    # 메인 프로세스에서만 출력 디렉토리와 로그 작성기 생성
    if misc.is_main_process() and config['output_dir']:
        output_dir = os.path.join(config['output_dir'], config['exp_name'])
        os.makedirs(output_dir, exist_ok=True)
        log_writer = SummaryWriter(log_dir=output_dir)
    else:
        output_dir = None
        log_writer = None

    # 모델 정의
    model_name = config['model_name']
    if model_name in models.__dict__:
        model = models.__dict__[model_name](**config['model'])
    else:
        # 지원되지 않는 모델 이름인 경우 예외 발생
        raise ValueError(f'Unsupported model name: {model_name}')
    # 모델을 지정된 장치로 이동
    model.to(device)

    # DDP가 적용되지 않은 모델 참조
    model_without_ddp = model
    print(f"Model = {model_without_ddp}")

    # 유효 배치 크기 계산
    eff_batch_size = config['dataloader']['batch_size'] * config['train']['accum_iter'] * misc.get_world_size()

    # 학습률 설정
    if config['train']['lr'] is None:
        config['train']['lr'] = config['train']['blr'] * eff_batch_size / 256

    # 학습률 정보 출력
    print(f"base lr: {config['train']['lr'] * 256 / eff_batch_size}")
    print(f"actual lr: {config['train']['lr']}")
    print(f"accumulate grad iterations: {config['train']['accum_iter']}")
    print(f"effective batch size: {eff_batch_size}")

    # 분산 학습 모드가 활성화된 경우 DDP로 모델 래핑
    if config['ddp']['distributed']:
        model = torch.nn.parallel.DistributedDataParallel(model,
                                                          device_ids=[config['ddp']['gpu']])
        model_without_ddp = model.module

    # 옵티마이저 생성
    optimizer = get_optimizer_from_config(config['train'], model_without_ddp)
    print(optimizer)
    # 손실 스케일러 초기화
    loss_scaler = NativeScaler()

    # 체크포인트에서 모델, 옵티마이저, 손실 스케일러 로드
    misc.load_model(config, model_without_ddp, optimizer, loss_scaler)

    # 학습 시작 메시지 출력
    print(f"Start training for {config['train']['epochs']} epochs")
    start_time = time.time()

    loss_list = []

    # 학습 루프
    for epoch in range(config['start_epoch'], config['train']['epochs']):
        # 분산 학습 모드에서 에포크 설정
        if config['ddp']['distributed']:
            data_loader_train.sampler.set_epoch(epoch)
        # 한 에포크 동안 학습
        train_stats = train_one_epoch(model,
                                      data_loader_train,
                                      optimizer,
                                      device,
                                      epoch,
                                      loss_scaler,
                                      log_writer,
                                      config['train'])
        # 주기적으로 체크포인트 저장
        if output_dir and (epoch % 20 == 0 or epoch + 1 == config['train']['epochs']):
            misc.save_model(config,
                            os.path.join(output_dir, f'checkpoint-{epoch}.pth'),
                            epoch,
                            model_without_ddp,
                            optimizer,
                            loss_scaler)

        # 학습 통계 로그 작성
        log_stats = {**{f'train_{k}': v for k, v in train_stats.items()},
                     'epoch': epoch,
                     }

        # 메인 프로세스에서만 로그 파일에 기록
        if output_dir and misc.is_main_process():
            if log_writer is not None:
                log_writer.flush()
            with open(os.path.join(output_dir, 'log.txt'), 'a', encoding="utf-8") as f:
                f.write(json.dumps(log_stats) + '\n')

    # 총 학습 시간 계산 및 출력
    total_time = time.time() - start_time
    total_time_str = str(datetime.timedelta(seconds=int(total_time)))
    print(f'Training time {total_time_str}')

    # 인코더 추출
    encoder = model_without_ddp.encoder
    # 인코더 저장
    if output_dir:
        misc.save_model(config,
                        os.path.join(output_dir, 'encoder_fft.pth'),
                        epoch,
                        encoder)
        
        # misc.save_model(config,
        #                 os.path.join(output_dir, 'encoder.pth'),
        #                 epoch,
        #                 encoder)


if __name__ == "__main__":
    config = parse()
    main(config)
