import os
import os.path as op
import mne
import mne.stats.cluster_level as mne_clusters
import nibabel as nib
import numpy as np
import time
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d import Axes3D
import shutil
import glob
import traceback
import subprocess

from src.utils import utils
from src.utils import freesurfer_utils as fu
from src.preproc import meg as meg
from src.utils import preproc_utils as pu
from src.utils import labels_utils as lu
from src.utils import args_utils as au
# from src.utils import qa_utils

try:
    from sklearn.neighbors import BallTree
except:
    print('No sklearn!')

try:
    from surfer import Brain
    from surfer import viz
    # from surfer import project_volume_data
    SURFER = True
except:
    SURFER = False
    print('no pysurfer!')


SUBJECTS_DIR, MMVT_DIR, FREESURFER_HOME = pu.get_links()
SUBJECTS_MEG_DIR = utils.get_link_dir(utils.get_links_dir(), 'meg')
FMRI_DIR = utils.get_link_dir(utils.get_links_dir(), 'fMRI')

FSAVG_VERTS = 10242
FSAVG5_VERTS = 163842

_bbregister = 'bbregister --mov {fsl_input}.nii --bold --s {subject} --init-fsl --lta register.lta'
_mri_robust_register = 'mri_robust_register --mov {fsl_input}.nii --dst $SUBJECTS_DIR/colin27/mri/orig.mgz' +\
                       ' --lta register.lta --satit --vox2vox --cost mi --mapmov {subject}_reg_mi.mgz'


def get_hemi_data(subject, hemi, source, surf_name='pial', name=None, sign="abs", min=None, max=None):
    brain = Brain(subject, hemi, surf_name, curv=False, offscreen=True)
    print('Brain {} verts: {}'.format(hemi, brain.geo[hemi].coords.shape[0]))
    hemi = brain._check_hemi(hemi)
    # load data here
    scalar_data, name = brain._read_scalar_data(source, hemi, name=name)
    print('fMRI contrast map vertices: {}'.format(len(scalar_data)))
    min, max = brain._get_display_range(scalar_data, min, max, sign)
    if sign not in ["abs", "pos", "neg"]:
        raise ValueError("Overlay sign must be 'abs', 'pos', or 'neg'")
    surf = brain.geo[hemi]
    old = viz.OverlayData(scalar_data, surf, min, max, sign)
    return old, brain


def calc_fmri_min_max(subject, contrast, hemis_files, norm_percs=(3, 97), norm_by_percentile=True):
    data = None
    for hemi_fname in hemis_files:
        hemi = 'rh' if '.rh' in hemi_fname else 'lh'
        fmri = nib.load(hemi_fname)
        x = fmri.get_data().ravel()
        verts, _ = utils.read_ply_file(op.join(MMVT_DIR, subject, 'surf', '{}.pial.ply'.format(hemi)))
        if x.shape[0] != verts.shape[0]:
            if x.shape[0] in [FSAVG5_VERTS, FSAVG_VERTS]:
                temp_barin = 'fsaverage5' if x.shape[0] == FSAVG5_VERTS else 'fsaverage'
                raise Exception(
                    "It seems that the fMRI contrast was made on {}, and not on the subject.\n".format(temp_barin) +
                    "You can run the fMRI preproc on the template barin, or morph the fMRI contrast map to the subject.")
            else:
                raise Exception("fMRI contrast map ({}) and the {} pial surface ({}) doesn't have the " +
                                "same vertices number!".format(len(x), hemi, verts.shape[0]))
        data = x if data is None else np.hstack((x, data))
    data_min, data_max = utils.calc_min_max(data, norm_percs=norm_percs, norm_by_percentile=norm_by_percentile)
    print('calc_fmri_min_max: min: {}, max: {}'.format(data_min, data_max))
    data_minmax = utils.get_max_abs(data_max, data_min)
    if args.symetric_colors and np.sign(data_max) != np.sign(data_min):
        data_max, data_min = data_minmax, -data_minmax
    # todo: the output_fname was changed, check where it's being used!
    output_fname = op.join(MMVT_DIR, subject, 'fmri','{}_minmax.pkl'.format(contrast))
    print('Saving {}'.format(output_fname))
    utils.make_dir(op.join(MMVT_DIR, subject, 'fmri'))
    utils.save((data_min, data_max), output_fname)


def save_fmri_hemi_data(subject, hemi, contrast_name, fmri_file, surf_name='pial', output_fol=''):
    if not op.isfile(fmri_file):
        print('No such file {}!'.format(fmri_file))
        return
    fmri = nib.load(fmri_file)
    x = fmri.get_data().ravel()
    if output_fol == '':
        output_fol = op.join(MMVT_DIR, subject, 'fmri')
    utils.make_dir(output_fol)
    output_name = op.join(output_fol, 'fmri_{}_{}'.format(contrast_name, hemi))
    _save_fmri_hemi_data(subject, hemi, x, output_name, surf_name=surf_name)


def _save_fmri_hemi_data(subject, hemi, x, output_file='', verts=None, surf_name='pial'):
    if verts is None:
        # Try to read the hemi ply file to check if the vertices number is correct
        verts, _ = utils.read_ply_file(op.join(MMVT_DIR, subject, 'surf', '{}.{}.ply'.format(hemi, surf_name)))
        if len(x) != verts.shape[0]:
            raise Exception("fMRI contrast map ({}) and the {} pial surface ({}) doesn't have the same vertices number!".format(len(x), hemi, verts.shape[0]))

    # colors = utils.arr_to_colors_two_colors_maps(x, cm_big='YlOrRd', cm_small='PuBu',
    #     threshold=threshold, default_val=1, norm_percs=norm_percs, norm_by_percentile=norm_by_percentile)
    # colors = np.hstack((x.reshape((len(x), 1)), colors))
    if output_file == '':
        output_file = op.join(MMVT_DIR, subject, 'fmri_{}.npy'.format(hemi))
    print('Saving {}'.format(output_file))
    np.save(output_file, x)


def init_clusters(subject, contrast_name, input_fol):
    input_fname = op.join(input_fol, 'fmri_{}_{}.npy'.format(contrast_name, '{hemi}'))
    contrast_per_hemi, verts_per_hemi = {}, {}
    for hemi in utils.HEMIS:
        fmri_fname = input_fname.format(hemi=hemi)
        if utils.file_type(input_fname) == 'npy':
            x = np.load(fmri_fname)
            contrast_per_hemi[hemi] = x #[:, 0]
        else:
            # try nibabel
            x = nib.load(fmri_fname)
            contrast_per_hemi[hemi] = x.get_data().ravel()
        pial_npz_fname = op.join(MMVT_DIR, subject, 'surf', '{}.pial.npz'.format(hemi))
        if not op.isfile(pial_npz_fname):
            print('No pial npz file (), creating one'.format(pial_npz_fname))
            verts, faces = utils.read_ply_file(op.join(MMVT_DIR, subject, 'surf', '{}.pial.ply'.format(hemi)))
            np.savez(pial_npz_fname[:-4], verts=verts, faces=faces)
        d = np.load(pial_npz_fname)
        verts_per_hemi[hemi] = d['verts']
    connectivity_fname = op.join(MMVT_DIR, subject, 'spatial_connectivity.pkl')
    if not op.isfile(connectivity_fname):
        from src.preproc import anatomy
        anatomy.create_spatial_connectivity(subject)
    connectivity_per_hemi = utils.load(connectivity_fname)
    return contrast_per_hemi, connectivity_per_hemi, verts_per_hemi


def find_clusters(subject, contrast_name, t_val, atlas, volume_name='', input_fol='', load_from_annotation=True, n_jobs=1):
    contrast_name = contrast_name if volume_name == '' else volume_name
    volume_name = volume_name if volume_name != '' else contrast_name
    if input_fol == '':
        input_fol = op.join(MMVT_DIR, subject, 'fmri')
    contrast, connectivity, verts = init_clusters(subject, contrast_name, input_fol)
    clusters_labels = dict(threshold=t_val, values=[])
    for hemi in utils.HEMIS:
        clusters, _ = mne_clusters._find_clusters(contrast[hemi], t_val, connectivity=connectivity[hemi])
        # blobs_output_fname = op.join(input_fol, 'blobs_{}_{}.npy'.format(contrast_name, hemi))
        # print('Saving blobs: {}'.format(blobs_output_fname))
        # save_clusters_for_blender(clusters, contrast[hemi], blobs_output_fname)
        clusters_labels_hemi = find_clusters_overlapped_labeles(
            subject, clusters, contrast[hemi], atlas, hemi, verts[hemi], load_from_annotation, n_jobs)
        if clusters_labels_hemi is None:
            print("Can't find clusters in {}!".format(hemi))
        else:
            clusters_labels['values'].extend(clusters_labels_hemi)

    clusters_labels_output_fname = op.join(
        MMVT_DIR, subject, 'fmri', 'clusters_labels_{}.pkl'.format(volume_name))
    print('Saving clusters labels: {}'.format(clusters_labels_output_fname))
    utils.save(clusters_labels, clusters_labels_output_fname)


def find_clusters_tval_hist(subject, contrast_name, output_fol, input_fol='', n_jobs=1):
    contrast, connectivity, _ = init_clusters(subject, contrast_name, input_fol)
    clusters = {}
    tval_values = np.arange(2, 20, 0.1)
    now = time.time()
    for ind, tval in enumerate(tval_values):
        try:
            # utils.time_to_go(now, ind, len(tval_values), 5)
            clusters[tval] = {}
            for hemi in utils.HEMIS:
                clusters[tval][hemi], _ = mne_clusters._find_clusters(
                    contrast[hemi], tval, connectivity=connectivity[hemi])
            print('tval: {:.2f}, len rh: {}, lh: {}'.format(tval, max(map(len, clusters[tval]['rh'])),
                                                        max(map(len, clusters[tval]['rh']))))
        except:
            print('error with tval {}'.format(tval))
    utils.save(clusters, op.join(output_fol, 'clusters_tval_hist.pkl'))


def load_clusters_tval_hist(input_fol):
    from itertools import chain
    clusters = utils.load(op.join(input_fol, 'clusters_tval_hist.pkl'))
    res = []
    for t_val, clusters_tval in clusters.items():
        tval = float('{:.2f}'.format(t_val))
        max_size = max([max([len(c) for c in clusters_tval[hemi]]) for hemi in utils.HEMIS])
        avg_size = np.mean(list(chain.from_iterable(([[len(c) for c in clusters_tval[hemi]] for hemi in utils.HEMIS]))))
        clusters_num = sum(map(len, [clusters_tval[hemi] for hemi in utils.HEMIS]))
        res.append((tval, max_size, avg_size, clusters_num))
    res = sorted(res)
    # res = sorted([(t_val, max([len(c) for c in [c_tval[hemi] for hemi in utils.HEMIS]])) for t_val, c_tval in clusters.items()])
    # tvals = [float('{:.2f}'.format(t_val)) for t_val, c_tval in clusters.items()]
    max_sizes = [r[1] for r in res]
    avg_sizes = [r[2] for r in res]
    tvals = [float('{:.2f}'.format(r[0])) for r in res]
    clusters_num = [r[3] for r in res]
    fig, ax1 = plt.subplots()
    ax1.plot(tvals, max_sizes, 'b')
    ax1.set_ylabel('max size', color='b')
    ax2 = ax1.twinx()
    ax2.plot(tvals, clusters_num, 'r')
    # ax2.plot(tvals, avg_sizes, 'g')
    ax2.set_ylabel('#clusters', color='r')
    plt.show()
    print('sdfsd')


def save_clusters_for_blender(clusters, contrast, output_file):
    vertices_num = len(contrast)
    data = np.ones((vertices_num, 4)) * -1
    colors = utils.get_spaced_colors(len(clusters))
    for ind, (cluster, color) in enumerate(zip(clusters, colors)):
        x = contrast[cluster]
        cluster_max = max([abs(np.min(x)), abs(np.max(x))])
        cluster_data = np.ones((len(cluster), 1)) * cluster_max
        cluster_color = np.tile(color, (len(cluster), 1))
        data[cluster, :] = np.hstack((cluster_data, cluster_color))
    np.save(output_file, data)


def find_clusters_overlapped_labeles(subject, clusters, contrast, atlas, hemi, verts, load_from_annotation=True,
                                     n_jobs=1):
    cluster_labels = []
    annot_fname = op.join(SUBJECTS_DIR, subject, 'label', '{}.{}.annot'.format(hemi, atlas))
    if load_from_annotation and op.isfile(annot_fname):
        labels = mne.read_labels_from_annot(subject, annot_fname=annot_fname, surf_name='pial')
    else:
        # todo: read only the labels from the current hemi
        labels = lu.read_labels_parallel(subject, SUBJECTS_DIR, atlas, hemi, n_jobs=n_jobs)
        labels = [l for l in labels if l.hemi == hemi]

    if len(labels) == 0:
        print('No labels!')
        return None
    for cluster in clusters:
        x = contrast[cluster]
        cluster_max = np.min(x) if abs(np.min(x)) > abs(np.max(x)) else np.max(x)
        inter_labels, inter_labels_tups = [], []
        for label in labels:
            overlapped_vertices = np.intersect1d(cluster, label.vertices)
            if len(overlapped_vertices) > 0:
                if 'unknown' not in label.name:
                    inter_labels_tups.append((len(overlapped_vertices), label.name))
                    # inter_labels.append(dict(name=label.name, num=len(overlapped_vertices)))
        inter_labels_tups = sorted(inter_labels_tups)[::-1]
        for inter_labels_tup in inter_labels_tups:
            inter_labels.append(dict(name=inter_labels_tup[1], num=inter_labels_tup[0]))
        if len(inter_labels) > 0:
            # max_inter = max([(il['num'], il['name']) for il in inter_labels])
            cluster_labels.append(dict(vertices=cluster, intersects=inter_labels, name=inter_labels[0]['name'],
                coordinates=verts[cluster], max=cluster_max, hemi=hemi, size=len(cluster)))
        else:
            print('No intersected labels!')
    return cluster_labels


def create_functional_rois(subject, contrast_name, clusters_labels_fname='', func_rois_folder=''):
    if clusters_labels_fname == '':
        clusters_labels = utils.load(op.join(
            MMVT_DIR, subject, 'fmri', 'clusters_labels_{}.npy'.format(contrast_name)))
    if func_rois_folder == '':
        func_rois_folder = op.join(SUBJECTS_DIR, subject, 'mmvt', 'fmri', 'functional_rois', '{}_labels'.format(contrast_name))
    utils.delete_folder_files(func_rois_folder)
    for cl in clusters_labels:
        cl_name = 'fmri_{}_{:.2f}'.format(cl['name'], cl['max'])
        new_label = mne.Label(cl['vertices'], cl['coordinates'], hemi=cl['hemi'], name=cl_name,
            filename=None, subject=subject, verbose=None)
        new_label.save(op.join(func_rois_folder, cl_name))


def show_fMRI_using_pysurfer(subject, input_file, hemi='both'):
    brain = Brain(subject, hemi, "pial", curv=False, offscreen=False)
    brain.toggle_toolbars(True)
    if hemi=='both':
        for hemi in ['rh', 'lh']:
            print('adding {}'.format(input_file.format(hemi=hemi)))
            brain.add_overlay(input_file.format(hemi=hemi), hemi=hemi)
    else:
        print('adding {}'.format(input_file.format(hemi=hemi)))
        brain.add_overlay(input_file.format(hemi=hemi), hemi=hemi)


def mri_convert_hemis(contrast_file_template, contrasts=None, existing_format='nii.gz'):
    for hemi in utils.HEMIS:
        if contrasts is None:
            contrasts = ['']
        for contrast in contrasts:
            if '{contrast}' in contrast_file_template:
                contrast_fname = contrast_file_template.format(hemi=hemi, contrast=contrast, format='{format}')
            else:
                contrast_fname = contrast_file_template.format(hemi=hemi, format='{format}')
            if not op.isfile(contrast_fname.format(format='mgz')):
                convert_fmri_file(contrast_fname, existing_format, 'mgz')


# def mri_convert(volume_fname, from_format='nii.gz', to_format='mgz'):
#     try:
#         print('convert {} to {}'.format(volume_fname.format(format=from_format), volume_fname.format(format=to_format)))
#         utils.run_script('mri_convert {} {}'.format(volume_fname.format(format=from_format),
#                                                     volume_fname.format(format=to_format)))
#     except:
#         print('Error running mri_convert!')


def convert_fmri_file(input_fname_template, from_format='nii.gz', to_format='mgz'):
    try:
        output_fname = input_fname_template.format(format=to_format)
        intput_fname = input_fname_template.format(format=from_format)
        output_files = glob.glob(output_fname)
        if len(output_files) == 0:
            inputs_files = glob.glob(intput_fname)
            if len(inputs_files) == 1:
                intput_fname = inputs_files[0]
                utils.run_script('mri_convert {} {}'.format(intput_fname, output_fname))
                return output_fname
            elif len(inputs_files) == 0:
                print('No imput file was found! {}'.format(intput_fname))
                return ''
            else:
                print('Too many input files were found! {}'.format(intput_fname))
                return ''
        else:
            return output_files[0]
    except:
        print('Error running mri_convert!')
        return ''


def calculate_subcorticals_activity(subject, volume_file, subcortical_codes_file='', aseg_stats_file_name='',
        method='max', k_points=100, do_plot=False):
    x = nib.load(volume_file)
    x_data = x.get_data()

    if do_plot:
        fig = plt.figure()
        ax = Axes3D(fig)

    sig_subs = []
    if subcortical_codes_file != '':
        subcortical_codes = np.genfromtxt(subcortical_codes_file, dtype=str, delimiter=',')
        seg_labels = map(str, subcortical_codes[:, 0])
    elif aseg_stats_file_name != '':
        aseg_stats = np.genfromtxt(aseg_stats_file_name, dtype=str, delimiter=',', skip_header=1)
        seg_labels = map(str, aseg_stats[:, 0])
    else:
        raise Exception('No segmentation file!')
    # Find the segmentation file
    aseg_fname = op.join(SUBJECTS_DIR, subject, 'mri', 'aseg.mgz')
    aseg = nib.load(aseg_fname)
    aseg_hdr = aseg.get_header()
    out_folder = op.join(SUBJECTS_DIR, subject, 'subcortical_fmri_activity')
    if not op.isdir(out_folder):
        os.mkdir(out_folder)
    sub_cortical_generator = utils.sub_cortical_voxels_generator(aseg, seg_labels, 5, False, FREESURFER_HOME)
    for pts, seg_name, seg_id in sub_cortical_generator:
        print(seg_name)
        verts, _ = utils.read_ply_file(op.join(SUBJECTS_DIR, subject, 'subcortical', '{}.ply'.format(seg_name)))
        vals = np.array([x_data[i, j, k] for i, j, k in pts])
        is_sig = np.max(np.abs(vals)) >= 2
        print(seg_name, seg_id, np.mean(vals), is_sig)
        pts = utils.transform_voxels_to_RAS(aseg_hdr, pts)
        # plot_points(verts,pts)
        verts_vals = calc_vert_vals(verts, pts, vals, method=method, k_points=k_points)
        print('verts vals: {}+-{}'.format(verts_vals.mean(), verts_vals.std()))
        if sum(abs(verts_vals)>2) > 0:
            sig_subs.append(seg_name)
        verts_colors = utils.arr_to_colors_two_colors_maps(verts_vals, threshold=2)
        verts_data = np.hstack((np.reshape(verts_vals, (len(verts_vals), 1)), verts_colors))
        np.save(op.join(out_folder, seg_name), verts_data)
        if do_plot:
            plot_points(verts, colors=verts_colors, fig_name=seg_name, ax=ax)
        # print(pts)
    utils.rmtree(op.join(MMVT_DIR, subject, 'subcortical_fmri_activity'))
    shutil.copytree(out_folder, op.join(MMVT_DIR, subject, 'subcortical_fmri_activity'))
    if do_plot:
        plt.savefig('/home/noam/subjects/mri/mg78/subcortical_fmri_activity/figures/brain.jpg')
        plt.show()


def calc_vert_vals(verts, pts, vals, method='max', k_points=100):
    ball_tree = BallTree(pts)
    dists, pts_inds = ball_tree.query(verts, k=k_points, return_distance=True)
    near_vals = vals[pts_inds]
    # sig_dists = dists[np.where(abs(near_vals)>2)]
    cover = len(np.unique(pts_inds.ravel()))/float(len(pts))
    print('{}% of the points are covered'.format(cover*100))
    if method=='dist':
        n_dists = 1/(dists**2)
        norm = 1/np.sum(n_dists, 1)
        norm = np.reshape(norm, (len(norm), 1))
        n_dists = norm * n_dists
        verts_vals = np.sum(near_vals * n_dists, 1)
    elif method=='max':
        verts_vals = near_vals[range(near_vals.shape[0]), np.argmax(abs(near_vals), 1)]
    return verts_vals


def plot_points(subject, verts, pts=None, colors=None, fig_name='', ax=None):
    if ax is None:
        fig = plt.figure()
        ax = Axes3D(fig)
    colors = 'tomato' if colors is None else colors
    # ax.plot(verts[:, 0], verts[:, 1], verts[:, 2], 'o', color=colors, label='verts')
    ax.scatter(verts[:, 0], verts[:, 1], verts[:, 2], s=20, c=colors, label='verts')
    if pts is not None:
        ax.plot(pts[:, 0], pts[:, 1], pts[:, 2], 'o', color='blue', label='voxels')
        plt.legend()
    if ax is None:
        plt.savefig(op.join(MMVT_DIR, subject, 'fmri', '{}.jpg'.format(fig_name)))
        plt.close()


def project_on_surface(subject, volume_file, colors_output_fname, surf_output_fname,
                       target_subject=None, overwrite_surf_data=False, is_pet=False):
    if target_subject is None:
        target_subject = subject
    utils.make_dir(op.join(MMVT_DIR, subject, 'fmri'))
    for hemi in utils.HEMIS:
        print('project {} to {}'.format(volume_file, hemi))
        if not op.isfile(surf_output_fname.format(hemi=hemi)) or overwrite_surf_data:
            if not is_pet:
                surf_data = fu.project_volume_data(volume_file, hemi, subject_id=subject, surf="pial", smooth_fwhm=3,
                    target_subject=target_subject, output_fname=surf_output_fname.format(hemi=hemi))
            else:
                surf_data = fu.project_pet_volume_data(subject, volume_file, hemi, surf_output_fname.format(hemi=hemi))
            nans = np.sum(np.isnan(surf_data))
            if nans > 0:
                print('there are {} nans in {} surf data!'.format(nans, hemi))
        else:
            surf_data = np.squeeze(nib.load(surf_output_fname.format(hemi=hemi)).get_data())
        output_fname = op.join(MMVT_DIR, subject, 'fmri', op.basename(colors_output_fname.format(hemi=hemi)))
        if not output_fname or overwrite_surf_data:
            np.save(output_fname, surf_data)


def load_images_file(image_fname):
    for hemi in ['rh', 'lh']:
        x = nib.load(image_fname.format(hemi=hemi))
        nans = np.sum(np.isnan(np.array(x.dataobj)))
        if nans > 0:
            print('there are {} nans in {} image!'.format(nans, hemi))


def mask_volume(volume, mask, masked_volume):
    vol_nib = nib.load(volume)
    vol_data = vol_nib.get_data()
    mask_nib = nib.load(mask)
    mask_data = mask_nib.get_data().astype(np.bool)
    vol_data[mask_data] = 0
    vol_nib.data = vol_data
    nib.save(vol_nib, masked_volume)


def load_and_show_npy(subject, npy_file, hemi):
    x = np.load(npy_file)
    brain = Brain(subject, hemi, "pial", curv=False, offscreen=False)
    brain.toggle_toolbars(True)
    brain.add_overlay(x[:, 0], hemi=hemi)


def copy_volume_to_blender(subject, volume_fname_template, contrast='', overwrite_volume_mgz=True):
    if op.isfile(volume_fname_template.format(format='mgh')) and \
            (not op.isfile(volume_fname_template.format(format='mgz')) or overwrite_volume_mgz):
        mri_convert(volume_fname_template, 'mgh', 'mgz')
        format = 'mgz'
    else:
        volume_files = glob.glob(op.join(volume_fname_template.replace('{format}', '*')))
        if len(volume_files) == 0:
            print('No volume file! Should be in {}'.format(volume_fname_template.replace('{format}', '*')))
            return ''
        if len(volume_files) > 1:
            print('Too many volume files!')
            return ''
        else:
            format = utils.file_type(volume_files[0])
    volume_fname = volume_fname_template.format(format=format)
    blender_volume_fname = op.basename(volume_fname) if contrast=='' else '{}.{}'.format(contrast, format)
    utils.make_dir(op.join(MMVT_DIR, subject, 'freeview'))
    shutil.copyfile(volume_fname, op.join(MMVT_DIR, subject, 'freeview', blender_volume_fname))
    return volume_fname


def project_volume_to_surface(subject, data_fol, volume_name, contrast, overwrite_surf_data=True,
                              overwrite_volume=True, target_subject=''):
    if os.environ.get('FREESURFER_HOME', '') == '':
        raise Exception('Source freesurfer and rerun')
    if target_subject == '':
        target_subject = subject
    volume_fname_template = op.join(data_fol, '{}.{}'.format(volume_name, '{format}'))
    # mri_convert_hemis(contrast_file_template, contrasts, existing_format=existing_format)
    volume_fname = copy_volume_to_blender(subject, volume_fname_template, contrast, overwrite_volume)
    target_subject_prefix = '_{}'.format(target_subject) if subject != target_subject else ''
    colors_output_fname = op.join(data_fol, 'fmri_{}{}_{}.npy'.format(volume_name, target_subject_prefix, '{hemi}'))
    surf_output_fname = op.join(data_fol, '{}{}_{}.mgz'.format(volume_name, target_subject_prefix, '{hemi}'))
        
    project_on_surface(subject, volume_fname, colors_output_fname, surf_output_fname,
                       target_subject, overwrite_surf_data=overwrite_surf_data, is_pet=args.is_pet)
    utils.make_dir(op.join(MMVT_DIR, subject, 'freeview'))
    # shutil.copy(volume_fname, op.join(MMVT_DIR, subject, 'freeview', op.basename(volume_fname)))

# fu.transform_mni_to_subject('colin27', data_fol, volume_fname, '{}_{}'.format(target_subject, volume_fname))
    # load_images_file(surf_output_fname)


def calc_meg_activity_for_functional_rois(subject, meg_subject, atlas, task, contrast_name, contrast, inverse_method):
    fname_format, fname_format_cond, events_id, event_digit = meg.get_fname_format(task)
    raw_cleaning_method = 'tsss' # 'nTSSS'
    files_includes_cond = True
    meg.init_globals(meg_subject, subject, fname_format, fname_format_cond, files_includes_cond, raw_cleaning_method, contrast_name,
        SUBJECTS_MEG_DIR, task, SUBJECTS_DIR, MMVT_DIR)
    root_fol = op.join(SUBJECTS_DIR, subject, 'mmvt', 'fmri', 'functional_rois')
    labels_fol = op.join(root_fol, '{}_labels'.format(contrast))
    labels_output_fname = op.join(root_fol, '{}_labels_data_{}'.format(contrast, '{hemi}'))
    # src = meg.create_smooth_src(subject)
    for hemi in ['rh', 'lh']:
        meg.calc_labels_avg_per_condition(atlas, hemi, 'pial', events_id, labels_from_annot=False,
            labels_fol=labels_fol, stcs=None, inverse_method=inverse_method,
            labels_output_fname_template=labels_output_fname)


def copy_volumes(subject, contrast_file_template, contrast, volume_fol, volume_name):
    contrast_format = 'mgz'
    volume_type = 'mni305'
    volume_file = contrast_file_template.format(contrast=contrast, hemi=volume_type, format='{format}')
    if not op.isfile(volume_file.format(format=contrast_format)):
        mri_convert(volume_file, 'nii.gz', contrast_format)
    volume_fname = volume_file.format(format=contrast_format)
    subject_volume_fname = op.join(volume_fol, '{}_{}'.format(subject, volume_name))
    if not op.isfile(subject_volume_fname):
        volume_fol, volume_name = op.split(volume_fname)
        fu.transform_mni_to_subject(subject, volume_fol, volume_name, '{}_{}'.format(subject, volume_name))
    blender_volume_fname = op.join(MMVT_DIR, subject, 'freeview', '{}.{}'.format(contrast, contrast_format))
    if not op.isfile(blender_volume_fname):
        print('copy {} to {}'.format(subject_volume_fname, blender_volume_fname))
        shutil.copyfile(subject_volume_fname, blender_volume_fname)


def analyze_resting_state(subject, atlas, fmri_file_template, measures=['mean'], rest_template='fsaverage', morph_from_subject='',
                          morph_to_subject='', overwrite=False, do_plot=False, do_plot_all_vertices=False,
                          excludes=('corpuscallosum', 'unknown'), input_format='nii.gz'):
    if fmri_file_template == '':
        print('You should set the fmri_file_template for something like ' +
              '{subject}.siemens.sm6.{morph_to_subject}.{hemi}.b0dc.{format}.\n' +
              'These files suppose to be located in {}'.format(op.join(FMRI_DIR, subject)))
        return False
    utils.make_dir(op.join(MMVT_DIR, subject, 'fmri'))
    fmri_file_template = op.join(FMRI_DIR, subject, fmri_file_template)
    morph_from_subject = subject if morph_from_subject == '' else morph_from_subject
    morph_to_subject = subject if morph_to_subject == '' else morph_to_subject
    figures_dir = op.join(FMRI_DIR, subject, 'figures')
    for hemi in utils.HEMIS:
        fmri_fname = convert_fmri_file(fmri_file_template.format(
            subject=subject, morph_to_subject=rest_template, hemi=hemi, format='{format}'),
            from_format=input_format)
        x = nib.load(fmri_fname).get_data()
        labels = lu.read_hemi_labels(morph_from_subject, SUBJECTS_DIR, atlas, hemi)
        if len(labels) == 0:
            print('No {} {} labels were found!'.format(morph_from_subject, atlas))
            return False
        # print(max([max(label.vertices) for label in labels]))
        for em in measures:
            output_fname = op.join(MMVT_DIR, subject, 'fmri', 'labels_data_{}_{}_{}.npz'.format(atlas, em, hemi))
            if op.isfile(output_fname) and not overwrite:
                print('{} already exist'.format(output_fname))
                return True
            labels_data, labels_names = lu.calc_time_series_per_label(
                x, labels, em, excludes, figures_dir, do_plot, do_plot_all_vertices)
            np.savez(output_fname, data=labels_data, names=labels_names)
            print('{} was saved'.format(output_fname))

    return np.all([utils.both_hemi_files_exist(op.join(MMVT_DIR, subject, 'fmri', 'labels_data_{}_{}_{}.npz'.format(
        atlas, em, '{hemi}'))) for em in measures])


def calc_labels_minmax(subject, atlas, extract_modes):
    for em in extract_modes:
        min_max_output_fname = op.join(MMVT_DIR, subject, 'fmri', 'labels_data_{}_{}_minmax.npy'.format(atlas, em))
        template = op.join(MMVT_DIR, subject, 'fmri', op.basename('labels_data_{}_{}_{}.npz'.format(atlas, em, '{hemi}')))
        if utils.both_hemi_files_exist(template):
            labels_data_rh = np.load(template.format(hemi='rh'))
            labels_data_lh = np.load(template.format(hemi='rh'))
            labels_min = min([np.min(labels_data_rh['data']), np.min(labels_data_lh['data'])])
            labels_max = max([np.max(labels_data_rh['data']), np.max(labels_data_lh['data'])])
            np.save(min_max_output_fname, [labels_min, labels_max])
        else:
            print("Can't find {}!".format(template))
    return np.all([op.isfile(op.join(MMVT_DIR, subject, 'fmri', 'labels_data_{}_{}_minmax.npy'.format(atlas, em)))
                   for em in extract_modes])

#
# def save_dynamic_activity_map(fmri_fname):
#     for hemi in HEMIS:
#         input_fname = fmri_file_template.format(
#             subject=subject, morph_to_subject=rest_template, hemi=hemi, format='{format}'
#         x = nib.load(fmri_fname).get_data()
#
#
#         verts, faces = utils.read_pial_npz(MRI_SUBJECT, MMVT_DIR, hemi)
#         data = stcs[hemi]
#         if verts.shape[0] != data.shape[0]:
#             raise Exception('save_activity_map: wrong number of vertices!')
#         else:
#             print('Both {}.pial.ply and the stc file have {} vertices'.format(hemi, data.shape[0]))
#         fol = '{}'.format(ACT.format(hemi))
#         utils.delete_folder_files(fol)
#         # data = data / data_max
#         now = time.time()
#         T = data.shape[1]
#         for t in range(T):
#             utils.time_to_go(now, t, T, runs_num_to_print=10)
#             np.save(op.join(fol, 't{}'.format(t)), data[:, t])


def clean_resting_state_data(subject, atlas, fmri_file_template, trg_subject='fsaverage5', fsd='rest',
                             fwhm=6, lfp=0.08, nskip=4, remote_fmri_dir='', overwrite=False, print_only=False):

    def find_files(fmri_file_template):
        return [f for f in glob.glob(fmri_file_template) if op.isfile(f) and utils.file_type(f) in ['mgz', 'nii.gz']
                and '_rh' not in utils.namebase(f) and '_lh' not in utils.namebase(f)]


    def get_fmri_fname(fmri_file_template):
        if fmri_file_template == '':
            fmri_file_template = '*rest*'
        full_fmri_file_template = op.join(FMRI_DIR, subject, fmri_file_template)
        files = find_files(full_fmri_file_template)
        files_num = len(set([utils.namebase(f) for f in files]))
        if files_num == 1:
            fmri_fname = files[0]
        elif files_num == 0:
            print('Trying to find remote files in {}'.format(op.join(remote_fmri_dir, fsd, '001', fmri_file_template)))
            files = find_files(op.join(remote_fmri_dir, fsd, '001', fmri_file_template)) + \
                    find_files(op.join(remote_fmri_dir, fmri_file_template))
            print('files: {}'.format(files))
            files_num = len(set([utils.namebase(f) for f in files]))
            if files_num == 1:
                fmri_fname = op.join(FMRI_DIR, subject, files[0].split(op.sep)[-1])
                utils.make_dir(op.join(FMRI_DIR, subject))
                shutil.copy(files[0], fmri_fname)
            else:
                raise Exception("Can't find any file in {}!".format(full_fmri_file_template))
        elif files_num > 1:
            raise Exception("More than one file can be found in {}! {}".format(full_fmri_file_template, files))
        return fmri_fname

    def create_folders_tree(fmri_fname):
        # Fisrt it's needed to create the freesurfer folders tree for the preproc-sess
        fol = utils.make_dir(op.join(FMRI_DIR, subject, fsd, '001'))
        if not op.isfile(op.join(fol, 'f.nii.gz')):
            if utils.file_type(fmri_fname) == 'mgz':
                fmri_fname = fu.mgz_to_nii_gz(fmri_fname)
            shutil.copy(fmri_fname, op.join(fol, 'f.nii.gz'))
        if not op.isfile(op.join(FMRI_DIR, subject, 'subjectname')):
            with open(op.join(FMRI_DIR, subject, 'subjectname'), 'w') as sub_file:
                sub_file.write(subject)

    def create_analysis_info_file(fsd, trg_subject, tr, fwhm=6, lfp=0.08, nskip=4):
        rs = utils.partial_run_script(locals(), cwd=FMRI_DIR, print_only=print_only)
        for hemi in utils.HEMIS:
            rs('mkanalysis-sess -analysis {fsd}_{hemi} -notask -TR {tr} -surface {trg_subject} {hemi} -fsd {fsd}' +
               ' -per-run -nuisreg global.waveform.dat 1 -nuisreg wm.dat 1 -nuisreg vcsf.dat 1 -lpf {lfp} -mcextreg' +
               ' -fwhm {fwhm} -nskip {nskip} -stc up -force', hemi=hemi)

    def find_trg_subject(trg_subject):
        if not op.isdir(op.join(SUBJECTS_DIR, trg_subject)):
            if op.isdir(op.join(FREESURFER_HOME, 'subjects', trg_subject)):
                os.symlink(op.join(FREESURFER_HOME, 'subjects', trg_subject),
                           op.join(SUBJECTS_DIR, trg_subject))
            else:
                raise Exception("The target subject {} doesn't exist!".format(trg_subject))

    def no_output(*args):
        return not op.isfile(op.join(FMRI_DIR, subject, fsd, *args))

    def run(cmd, *output_args, **kargs):
        if no_output(*output_args) or overwrite:
            rs(cmd, **kargs)
            if no_output(*output_args):
                raise Exception('{}\nNo output created in {}!!\n\n'.format(
                    cmd, op.join(FMRI_DIR, subject, fsd, *output_args)))


    if os.environ.get('FREESURFER_HOME', '') == '':
        raise Exception('Source freesurfer and rerun')
    find_trg_subject(trg_subject)
    fmri_fname = get_fmri_fname(fmri_file_template)
    create_folders_tree(fmri_fname)
    rs = utils.partial_run_script(locals(), cwd=FMRI_DIR, print_only=print_only)
    # if no_output('001', 'fmcpr.sm{}.mni305.2mm.nii.gz'.format(int(fwhm))):
    run('preproc-sess -surface {trg_subject} lhrh -s {subject} -fwhm {fwhm} -fsd {fsd} -mni305 -per-run',
        '001', 'fmcpr.sm{}.mni305.2mm.nii.gz'.format(int(fwhm)))
    run('plot-twf-sess -s {subject} -dat f.nii.gz -mc -fsd {fsd} && killall display', 'fmcpr.mcdat.png')
    run('plot-twf-sess -s {subject} -dat f.nii.gz -fsd {fsd} -meantwf && killall display', 'global.waveform.dat.png')

    # registration
    run('tkregister-sess -s {subject} -per-run -fsd {fsd} -bbr-sum > {subject}/{fsd}/reg_quality.txt',
        'reg_quality.txt')

    # Computes seeds (regressors) that can be used for functional connectivity analysis or for use as nuisance regressors.
    if no_output('001', 'wm.dat'):
        rs('fcseed-config -wm -overwrite -fcname wm.dat -fsd {fsd} -cfg {subject}/wm_{fsd}.cfg')
        run('fcseed-sess -s {subject} -cfg {subject}/wm_{fsd}.cfg', '001', 'wm.dat')
    if no_output('001', 'vcsf.dat'):
        rs('fcseed-config -vcsf -overwrite -fcname vcsf.dat -fsd {fsd} -mean -cfg {subject}/vcsf_{fsd}.cfg')
        run('fcseed-sess -s {subject} -cfg {subject}/vcsf_{fsd}.cfg', '001', 'vcsf.dat')

    tr = get_tr(subject, fmri_fname) / 1000 # To sec
    create_analysis_info_file(fsd, trg_subject, tr, fwhm, lfp, nskip)
    for hemi in utils.HEMIS:
        # computes the average signal intensity maps
        run('selxavg3-sess -s {subject} -a {fsd}_{hemi} -svres -no-con-ok',
            '{}_{}'.format(fsd, hemi), 'res', 'res-001.nii.gz', hemi=hemi)

    for hemi in utils.HEMIS:
        # new_fname = utils.add_str_to_file_name(fmri_fname, '_{}'.format(hemi))
        new_fname = op.join(FMRI_DIR, subject, '{}.sm{}.{}.{}.mgz'.format(fsd, int(fwhm), trg_subject, hemi))
        if not op.isfile(new_fname):
            res_fname = op.join(FMRI_DIR, subject, fsd, '{}_{}'.format(fsd, hemi), 'res', 'res-001.nii.gz')
            fu.nii_gz_to_mgz(res_fname)
            res_fname = utils.change_fname_extension(res_fname, 'mgz')
            shutil.copy(res_fname, new_fname)


def get_tr(subject, fmri_fname):
    try:
        tr_fname = utils.add_str_to_file_name(fmri_fname, '_tr', 'pkl')
        if op.isfile(tr_fname):
            return utils.load(tr_fname)
        if utils.is_file_type(fmri_fname, 'nii.gz'):
            old_fmri_fname = fmri_fname
            fmri_fname = '{}mgz'.format(fmri_fname[:-len('nii.gz')])
            if not op.isfile(fmri_fname):
                fu.mri_convert(old_fmri_fname, fmri_fname)
        if utils.is_file_type(fmri_fname, 'mgz'):
            fmri_fname = op.join(FMRI_DIR, subject, fmri_fname)
            tr = fu.get_tr(fmri_fname)
            # print('fMRI fname: {}'.format(fmri_fname))
            print('tr: {}'.format(tr))
            utils.save(tr, tr_fname)
            return tr
        else:
            print('file format not supported!')
            return None
    except:
        print(traceback.format_exc())
        return None


def fmri_pipeline(subject, atlas, contrast_file_template, t_val=2, surface_name='pial', contrast_format='mgz',
         existing_format='nii.gz', fmri_files_fol='', load_labels_from_annotation=True, volume_type='mni305', n_jobs=2):
    '''

    Parameters
    ----------
    subject: subject's name
    atlas: pacellation name
    contrast_file_template: template for the contrast file name. To get a full name the user should run:
          contrast_file_template.format(hemi=hemi, constrast=constrast, format=format)
    t_val: tval cutt off for finding clusters
    surface_name: Just for output name
    contrast_format: The contrast format (mgz, nii, nii.gz, ...)
    existing_format: The exsiting format (mgz, nii, nii.gz, ...)
    fmri_files_fol: The fmri files output folder
    load_labels_from_annotation: For finding the intersected labels, if True the function tries to read the labels from
        the annotation file, if False it tries to read the labels files.
    Returns
    -------

    '''
    from collections import defaultdict
    fol = op.join(FMRI_DIR, args.task, subject)
    contrasts = set([utils.namebase(f) for f in glob.glob(op.join(fol, 'bold', '*'))])
    # if len(contrast_names) > 1:
    #     raise Exception('More than one contrast found in {}, you should set the contrast_name flag.'.format(fol))
    # if len(contrast_names) == 0:
    #     raise Exception('No contrast found in {}!'.format(fol))
    # contrast_name = contrast_names[0]
    # contrast_files = glob.glob(op.join(fol, '**', 'sig.*'), recursive=True)
    for contrast in contrasts:
        contrast_files = glob.glob(op.join(fol, 'bold', '*{}*'.format(contrast), 'sig.*'), recursive=True)
        contrast_files_dic = defaultdict(list)
        for contrast_file in contrast_files:
            ft = utils.file_type(contrast_file)
            contrast_files_dic[contrast_file[:-len(ft) - 1]].append(ft)
        for contrast_file, fts in contrast_files_dic.items():
            if 'mgz' not in fts:
                fu.mri_convert('{}.{}'.format(contrast_file, fts[0]), '{}.mgz'.format(contrast_file))
        # sm = glob.glob(op.join(fol, 'bold', '{}*'.format(contrast)))[0].split('.')[1]
        # volume_type = [f for f in glob.glob(op.join(fol, 'bold', '{}*'.format(contrast)))
        #                if 'lh' not in f and 'rh' not in f][0].split('.')[-1]
        contrast_files = ['{}.mgz'.format(contrast_file) for contrast_file in contrast_files_dic.keys()]
        volume_files = [f for f in contrast_files if 'lh' not in f and 'rh' not in f]
        utils.make_dir(op.join(MMVT_DIR, subject, 'freeview'))
        for volume_fname in volume_files:
            shutil.copyfile(volume_fname, op.join(MMVT_DIR, subject, 'freeview', '{}.{}'.format(contrast, format)))
        hemis_files = [f for f in contrast_files if 'lh' in f or 'rh' in f]
        calc_fmri_min_max(
            subject, contrast, hemis_files, norm_percs=args.norm_percs,
            norm_by_percentile=args.norm_by_percentile)
        for hemi_fname in hemis_files:
            hemi = 'rh' if '.rh' in hemi_fname else 'lh'
            save_fmri_hemi_data(subject, hemi, contrast, hemi_fname, surface_name,
                             output_fol=fmri_files_fol)
        find_clusters(subject, contrast, t_val, atlas, '', fmri_files_fol, load_labels_from_annotation, n_jobs)
    # if contrasts is None and '{contrast}' in contrast_file_template:
    #     contrasts_fol = op.sep.join(contrast_file_template.split(op.sep)[:-2]).format(hemi='rh')
    #     contrasts = set([op.sep.join(c.split(op.sep)[-1:]).split('.')[0] for c in glob.glob(op.join(utils.get_parent_fol(contrasts_fol), '*'))])
    # Check if the contrast is in mgz, and if not convert it to mgz
    # mri_convert_hemis(contrast_file_template, contrasts, existing_format=existing_format)
    if contrasts is None:
        contrasts = ['group-avg']
    # for contrast in contrasts:
        # if '{contrast}' in contrast_file_template:
        #     contrast_file = contrast_file_template.format(contrast=contrast, hemi='{hemi}', format=contrast_format)
        #     volume_file = contrast_file_template.format(contrast=contrast, hemi=volume_type, format='{format}')
        # else:
        #     contrast_file = contrast_file_template.format(hemi='{hemi}', format=contrast_format)
        #     volume_file = contrast_file_template.format(hemi=volume_type, format='{format}')
        # copy_volume_to_blender(subject, volume_file, contrast, overwrite_volume_mgz=True)
        # calc_fmri_min_max(
        #     subject, contrast, contrast_file_template, norm_percs=args.norm_percs,
        #     norm_by_percentile=args.norm_by_percentile)
        # for hemi in ['rh', 'lh']:
        #     save_fmri_hemi_data(subject, hemi, contrast, contrast_file.format(hemi=hemi), surface_name,
        #                      output_fol=fmri_files_fol)
        # Find the fMRI blobs (clusters of activation)
        # find_clusters(subject, contrast, t_val, atlas, '', fmri_files_fol, load_labels_from_annotation, n_jobs)
        # Create functional rois out of the blobs
        # create_functional_rois(subject, contrast)
    # todo: check what to return
    return True


def misc(args):
    contrast_name = 'interference'
    contrasts = {'non-interference-v-base': '-a 1', 'interference-v-base': '-a 2',
                 'non-interference-v-interference': '-a 1 -c 2', 'task.avg-v-base': '-a 1 -a 2'}
    fol = op.join(FMRI_DIR, args.task, args.subject[0])
    contrast_file_template = op.join(fol, 'bold',
        '{contrast_name}.sm05.{hemi}'.format(contrast_name=contrast_name, hemi='{hemi}'), '{contrast}', 'sig.{format}')
    # contrast_file_template = op.join(fol, 'sig.{hemi}.{format}')


    contrast_name = 'group-avg'
    # main(subject, atlas, None, contrast_file_template, t_val=14, surface_name='pial', existing_format='mgh')
    # find_clusters_tval_hist(subject, contrast_name, fol, input_fol='', n_jobs=1)
    # load_clusters_tval_hist(fol)

    # contrast = 'non-interference-v-interference'
    inverse_method = 'dSPM'
    # meg_subject = 'ep001'

    # overwrite_volume_mgz = False
    # data_fol = op.join(FMRI_DIR, task, 'healthy_group')
    # contrast = 'pp003_vs_healthy'
    # contrast = 'pp009_ARC_High_Risk_Linear_Reward_contrast'
    # contrast = 'pp009_ARC_PPI_highrisk_L_VLPFC'

    # create_functional_rois(subject, contrast, data_fol)

    # # todo: find the TR automatiaclly
    # TR = 1.75

    # show_fMRI_using_pysurfer(subject, '/homes/5/npeled/space3/fMRI/ECR/hc004/bold/congruence.sm05.lh/congruent-v-incongruent/sig.mgz', 'rh')

    # fsfast.run(subject, root_dir=ROOT_DIR, par_file = 'msit.par', contrast_name=contrast_name, tr=TR, contrasts=contrasts, print_only=False)
    # fsfast.plot_contrast(subject, ROOT_DIR, contrast_name, contrasts, hemi='rh')
    # mri_convert_hemis(contrast_file_template, list(contrasts.keys())


    # show_fMRI_using_pysurfer(subject, input_file=contrast_file, hemi='lh')
    # root = op.join('/autofs/space/franklin_003/users/npeled/fMRI/MSIT/pp003')
    # volume_file = op.join(root, 'sig.anat.mgz')
    # mask_file = op.join(root, 'VLPFC.mask.mgz')
    # masked_file = op.join(root, 'sig.anat.masked.mgz')
    # contrast_file = op.join(root, 'sig.{hemi}.mgz')
    # contrast_masked_file = op.join(root, 'sig.masked.{hemi}.mgz')

    # for hemi in ['rh', 'lh']:
    #     save_fmri_colors(subject, hemi, contrast_masked_file.format(hemi=hemi), 'pial', threshold=2)
    # Show the fRMI in pysurfer
    # show_fMRI_using_pysurfer(subject, input_file=contrast_masked_file, hemi='both')

    # load_and_show_npy(subject, '/homes/5/npeled/space3/visualization_blender/mg79/fmri_lh.npy', 'lh')

    # mask_volume(volume_file, mask_file, masked_file)
    # show_fMRI_using_pysurfer(subject, input_file='/autofs/space/franklin_003/users/npeled/fMRI/MSIT/pp003/sig.{hemi}.masked.mgz', hemi='both')
    # calculate_subcorticals_activity(subject, '/homes/5/npeled/space3/MSIT/mg78/bold/interference.sm05.mni305/non-interference-v-interference/sig.anat.mgh',
    #              '/autofs/space/franklin_003/users/npeled/MSIT/mg78/aseg_stats.csv')
    # calculate_subcorticals_activity(subject, '/home/noam/fMRI/MSIT/mg78/bold/interference.sm05.mni305/non-interference-v-interference/sig.anat.mgh',
    #              '/home/noam/fMRI/MSIT/mg78/aseg_stats.csv')
    # volume_file = nib.load('/autofs/space/franklin_003/users/npeled/fMRI/MSIT/mg78/bold/interference.sm05.mni305/non-interference-v-interference/sig_subject.mgz')
    # vol_data, vol_header = volume_file.get_data(), volume_file.get_header()

    # contrast_file=contrast_file_template.format(
    #     contrast='non-interference-v-interference', hemi='mni305', format='mgz')
    # calculate_subcorticals_activity(subject, volume_file, subcortical_codes_file=op.join(BLENDER_DIR, 'sub_cortical_codes.txt'),
    #     method='dist')

    # SPM_ROOT = '/homes/5/npeled/space3/spm_subjects'
    # for subject_fol in utils.get_subfolders(SPM_ROOT):
    #     subject = utils.namebase(subject_fol)
    #     print(subject)
    #     contrast_masked_file = op.join(subject_fol, '{}_VLPFC_{}.mgz'.format(subject, '{hemi}'))
    #     show_fMRI_using_pysurfer(subject, input_file=contrast_masked_file, hemi='rh')
    # brain = Brain('fsaverage', 'both', "pial", curv=False, offscreen=False)


def main(subject, remote_subject_dir, args, flags):
    volume_name = args.volume_name if args.volume_name != '' else subject
    fol = op.join(FMRI_DIR, args.task, subject)
    remote_fmri_dir = remote_subject_dir if args.remote_fmri_dir == '' else \
        utils.build_remote_subject_dir(args.remote_fmri_dir, subject)
    if args.fsfast:
        fmri_contrast_file_template = op.join(fol, 'bold', '{contrast_name}.sm05.{hemi}'.format(
            contrast_name=args.contrast_name, hemi='{hemi}'), '{contrast}', 'sig.{format}')
    else:
        fmri_contrast_file_template = op.join(fol, '{}_{}.mgz'.format(volume_name, '{hemi}'))

    # todo: should find automatically the existing_format
    if 'fmri_pipeline' in args.function:
        flags['fmri_pipeline'] = fmri_pipeline(
            subject, args.atlas, fmri_contrast_file_template, t_val=args.threshold,
            existing_format=args.existing_format, volume_type=args.volume_type, load_labels_from_annotation=True,
            surface_name=args.surface_name, n_jobs=args.n_jobs)

    if utils.should_run(args, 'project_volume_to_surface'):
        flags['project_volume_to_surface'] = project_volume_to_surface(
            subject, fol, volume_name, args.contrast, args.overwrite_surf_data, args.overwrite_volume)

    if utils.should_run(args, 'calc_fmri_min_max'):
        #todo: won't work, need to find the hemis files first
        flags['calc_fmri_min_max'] = calc_fmri_min_max(
            subject, volume_name, fmri_contrast_file_template, norm_percs=args.norm_percs,
            norm_by_percentile=args.norm_by_percentile)

    if utils.should_run(args, 'find_clusters'):
        flags['find_clusters'] = find_clusters(subject, args.contrast, args.threshold, args.atlas, volume_name)

    if 'analyze_resting_state' in args.function:
        flags['analyze_resting_state'] = analyze_resting_state(
            subject, args.atlas, args.fmri_file_template, args.labels_extract_mode, args.rest_template, args.morph_labels_from_subject,
            args.morph_labels_to_subject, args.overwrite_labels_data, args.resting_state_plot,
            args.resting_state_plot_all_vertices, args.excluded_labels, args.input_format)

    if 'calc_labels_minmax' in args.function:
        flags['calc_labels_minmax'] = calc_labels_minmax(subject, args.atlas, args.labels_extract_mode)

    if 'clean_resting_state_data' in args.function:
        clean_resting_state_data(subject, args.atlas, args.fmri_file_template, args.rest_template,
                                 remote_fmri_dir=remote_fmri_dir)

    if 'calc_meg_activity' in args.function:
        meg_subject = args.meg_subject
        if meg_subject == '':
            print('You must set MEG subject (--meg_subject) to run calc_meg_activity function!')
        else:
            flags['calc_meg_activity'] = calc_meg_activity_for_functional_rois(
                subject, meg_subject, args.atlas, args.task, args.contrast_name, args.contrast, args.inverse_method)

    if 'copy_volumes' in args.function:
        flags['copy_volumes'] = copy_volumes(subject, fmri_contrast_file_template)

    if 'get_tr' in args.function:
        tr = get_tr(subject, args.fmri_fname)
        flags['get_tr'] = not tr is None

    return flags


def read_cmd_args(argv=None):
    import argparse
    from src.utils import args_utils as au

    parser = argparse.ArgumentParser(description='Description of your program')
    parser.add_argument('-c', '--contrast', help='contrast map', required=False, default='')
    parser.add_argument('-n', '--contrast_name', help='contrast map', required=False, default='')
    parser.add_argument('-t', '--task', help='task', required=False, default='')
    parser.add_argument('--threshold', help='clustering threshold', required=False, default=2, type=float)
    parser.add_argument('--fsfast', help='', required=False, default=1, type=au.is_true)
    parser.add_argument('--is_pet', help='', required=False, default=0, type=au.is_true)
    parser.add_argument('--existing_format', help='existing format', required=False, default='mgz')
    parser.add_argument('--input_format', help='input format', required=False, default='nii.gz')
    parser.add_argument('--volume_type', help='volume type', required=False, default='mni305')
    parser.add_argument('--volume_name', help='volume file name', required=False, default='')
    parser.add_argument('--surface_name', help='surface_name', required=False, default='pial')
    parser.add_argument('--meg_subject', help='meg_subject', required=False, default='')
    parser.add_argument('--inverse_method', help='inverse method', required=False, default='dSPM')

    parser.add_argument('--overwrite_surf_data', help='', required=False, default=0, type=au.is_true)
    parser.add_argument('--overwrite_colors_file', help='', required=False, default=0, type=au.is_true)
    parser.add_argument('--overwrite_volume', help='', required=False, default=0, type=au.is_true)

    parser.add_argument('--norm_by_percentile', help='', required=False, default=1, type=au.is_true)
    parser.add_argument('--norm_percs', help='', required=False, default='1,99', type=au.int_arr_type)
    parser.add_argument('--symetric_colors', help='', required=False, default=1, type=au.is_true)
    parser.add_argument('--remote_fmri_dir', help='', required=False, default='')

    # Resting state flags
    parser.add_argument('--fmri_file_template', help='', required=False, default='')
    parser.add_argument('--labels_extract_mode', help='', required=False, default='mean', type=au.str_arr_type)
    parser.add_argument('--morph_labels_from_subject', help='', required=False, default='fsaverage')
    parser.add_argument('--morph_labels_to_subject', help='', required=False, default='')
    parser.add_argument('--resting_state_plot', help='', required=False, default=0, type=au.is_true)
    parser.add_argument('--resting_state_plot_all_vertices', help='', required=False, default=0, type=au.is_true)
    parser.add_argument('--excluded_labels', help='', required=False, default='corpuscallosum,unknown', type=au.str_arr_type)
    parser.add_argument('--overwrite_labels_data', help='', required=False, default=0, type=au.is_true)
    # parser.add_argument('--raw_fwhm', help='Raw Full Width at Half Maximum for Spatial Smoothing', required=False, default=5, type=float)
    parser.add_argument('--rest_template', help='', required=False, default='fsaverage5')


    # Misc flags
    parser.add_argument('--fmri_fname', help='', required=False, default='')
    pu.add_common_args(parser)
    args = utils.Bag(au.parse_parser(parser, argv))
    args.necessary_files = {'surf': ['lh.sphere.reg', 'rh.sphere.reg']}
    if 'clean_resting_state_data' in args.function or args.function == 'prepare_subject_folder':
        args.necessary_files = {'surf': ['rh.thickness', 'lh.thickness', 'rh.white', 'lh.white', 'lh.sphere.reg', 'rh.sphere.reg'],
                                'mri': ['brainmask.mgz', 'orig.mgz', 'aparc+aseg.mgz'],
                                'mri:transforms': ['talairach.xfm']}
        # 'label': ['lh.cortex.label', 'rh.cortex.label']
    if args.is_pet:
        args.fsfast = False
    # print(args)
    for sub in args.subject:
        if '*' in sub:
            args.subject.remove(sub)
            args.subject.extend([fol.split(op.sep)[-1] for fol in glob.glob(op.join(FMRI_DIR, sub))])
    return args


if __name__ == '__main__':
    args = read_cmd_args()
    pu.run_on_subjects(args, main)
    print('finish!')



