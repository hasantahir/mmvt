import os.path as op
import glob

from src.utils import utils
from src.utils import preproc_utils as pu
from src.utils import args_utils as au
from src.utils import freesurfer_utils as fu


SUBJECTS_MRI_DIR, MMVT_DIR, FREESURFER_HOME = pu.get_links()


def do_something(subject, args):
    pass


def main(subject, remote_subject_dir, args, flags):
    if utils.should_run(args, 'do_something'):
        flags['do_something'] = do_something(subject, args)
    return flags


def read_cmd_args(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description='MMVT template preprocessing')
    parser.add_argument('--flag', help='', required=False, default='')
    pu.add_common_args(parser)
    args = utils.Bag(au.parse_parser(parser, argv))
    return args


if __name__ == '__main__':
    args = read_cmd_args()
    pu.run_on_subjects(args, main)
    print('finish!')
