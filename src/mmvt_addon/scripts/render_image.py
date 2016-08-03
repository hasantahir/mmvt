import sys
import os
import os.path as op


try:
    from src.mmvt_addon.scripts import scripts_utils as su
except:
    # Add current folder the imports path
    sys.path.append(os.path.split(__file__)[0])
    import scripts_utils as su


def wrap_blender_call():
    args = read_args()
    su.call_script(__file__, args)


def read_args(argv=None):
    parser = su.add_default_args()
    parser.add_argument('-o', '--output_path', help='output path', required=False, default='')
    parser.add_argument('-i', '--image_name', help='image name', required=False, default='')
    parser.add_argument('-q', '--quality', help='render quality', required=False, default=60, type=int)
    parser.add_argument('--smooth_figure', help='smooth figure', required=False, default=False, type=su.is_true)
    parser.add_argument('--hide_lh', help='hide left hemi', required=False, default=False, type=su.is_true)
    parser.add_argument('--hide_rh', help='hide right hemi', required=False, default=False, type=su.is_true)
    parser.add_argument('--hide_subs', help='hide sub corticals', required=False, default=False, type=su.is_true)
    return su.parse_args(parser, argv)


def render_image(subject_fname):
    args = read_args(su.get_python_argv())
    if args.output_path == '':
        mmvt_dir = op.join(su.get_links_dir(), 'mmvt')
        args.output_path = op.join(mmvt_dir, args.subject, 'figures')
    su.make_dir(args.output_path)
    mmvt = su.init_mmvt_addon()
    mmvt.show_hide_hemi(args.hide_lh, 'lh')
    mmvt.show_hide_hemi(args.hide_rh, 'rh')
    mmvt.show_hide_sub_corticals(args.hide_subs)
    mmvt.set_render_quality(args.quality)
    mmvt.set_render_output_path(args.output_path)
    mmvt.set_render_smooth_figure(args.smooth_figure)
    if op.isfile(op.join(args.output_path, 'camera.pkl')):
        mmvt.load_camera()
    else:
        cont = input('No camera file was detected in the output folder, continue?')
        if not su.is_true(cont):
            return
    su.save_blend_file(subject_fname)
    mmvt.render_image(args.image_name, args.output_path, args.quality, args.smooth_figure)
    su.exit_blender()


if __name__ == '__main__':
    import sys
    subject_fname = sys.argv[1]
    if sys.argv[2] == '--background':
        render_image(subject_fname)
    else:
        wrap_blender_call()
