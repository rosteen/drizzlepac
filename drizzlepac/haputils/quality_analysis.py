"""Code that evaluates the quality of products generated by the drizzlepac package.

The JSON files generated here can be converted directly into a Pandas DataFrame
using the syntax:

>>> import json
>>> import pandas as pd
>>> with open("<rootname>_astrometry_resids.json") as jfile:
>>>     resids = json.load(jfile)
>>> pdtab = pd.DataFrame(resids)

These DataFrames can then be concatenated using:

>>> allpd = pdtab.concat([pdtab2, pdtab3])

where 'pdtab2' and 'pdtab3' are DataFrames generated from other datasets.  For
more information on how to merge DataFrames, see 

https://pandas.pydata.org/pandas-docs/stable/user_guide/merging.html

Visualization of these Pandas DataFrames with Bokeh can follow the example
from:

https://programminghistorian.org/en/lessons/visualizing-with-bokeh


"""
import json
import os
import sys
from datetime import datetime
import time

from astropy.table import Table, vstack
from astropy.io import fits
from astropy.stats import sigma_clipped_stats
import numpy as np

from stwcs.wcsutil import HSTWCS
from stsci.tools.fileutil import countExtn
from stsci.tools import logutil
import tweakwcs

from . import astrometric_utils as amutils
from .. import tweakutils
from . import diagnostic_utils as du

MSG_DATEFMT = '%Y%j%H%M%S'
SPLUNK_MSG_FORMAT = '%(asctime)s %(levelname)s src=%(name)s- %(message)s'
log = logutil.create_logger(__name__, level=logutil.logging.NOTSET, stream=sys.stdout,
                            format=SPLUNK_MSG_FORMAT, datefmt=MSG_DATEFMT)

__taskname__ = 'qa_astrometry'


def determine_alignment_residuals(input, files, max_srcs=None,
                                  json_timestamp=None, 
                                  json_time_since_epoch=None,
                                  log_level=logutil.logging.NOTSET):
    """Determine the relative alignment between members of an association.

    Parameters
    -----------
    input : string
        Original pipeline input filename.  This filename will be used to
        define the output analysis results filename.

    files : list
        Set of files on which to actually perform comparison.  The original
        pipeline can work on both CTE-corrected and non-CTE-corrected files,
        but this comparison will only be performed on CTE-corrected
        products when available.

    json_timestamp: str, optional
        Universal .json file generation date and time (local timezone) that will be used in the instantiation
        of the HapDiagnostic object. Format: MM/DD/YYYYTHH:MM:SS (Example: 05/04/2020T13:46:35). If not
        specified, default value is logical 'None'

    json_time_since_epoch : float
        Universal .json file generation time that will be used in the instantiation of the HapDiagnostic
        object. Format: Time (in seconds) elapsed since January 1, 1970, 00:00:00 (UTC). If not specified,
        default value is logical 'None'

    log_level : int, optional
        The desired level of verboseness in the log statements displayed on the screen and written to the
        .log file. Default value is 'NOTSET'.

    Returns
    --------
    resids_files : list of string
        Name of JSON files containing all the extracted results from the comparisons
        being performed.
    """
    log.setLevel(log_level)
    
    # Open all files as HDUList objects
    hdus = [fits.open(f) for f in files]
    # Determine sources from each chip
    src_cats = []
    num_srcs = []
    for hdu in hdus:
        numsci = countExtn(hdu)
        nums = 0
        img_cats = {}
        for chip in range(numsci):
            chip += 1
            img_cats[chip] = amutils.extract_point_sources(hdu[("SCI", chip)].data, nbright=max_srcs)
            nums += len(img_cats[chip])
        num_srcs.append(nums)
        src_cats.append(img_cats)

    if len(num_srcs) == 0 or (len(num_srcs) > 0 and  max(num_srcs) <= 3):
        log.warning("Not enough sources identified in input images for comparison")
        return None

    # src_cats = [amutils.generate_source_catalog(hdu) for hdu in hdus]
    # Combine WCS from HDULists and source catalogs into tweakwcs-compatible input
    imglist = []
    for i, (f, cat) in enumerate(zip(files, src_cats)):
        imglist += amutils.build_wcscat(f, i, cat)

    # Setup matching algorithm using parameters tuned to well-aligned images
    match = tweakwcs.TPMatch(searchrad=5, separation=4.0,
                             tolerance=1.0, use2dhist=True)
    try:
        # perform relative fitting
        matchlist = tweakwcs.align_wcs(imglist, None, match=match, expand_refcat=False)
        del matchlist
    except Exception:
        try:
            # Try without 2dHist use to see whether we can get any matches at all
            match = tweakwcs.TPMatch(searchrad=5, separation=4.0,
                                     tolerance=1.0, use2dhist=False)
            matchlist = tweakwcs.align_wcs(imglist, None, match=match, expand_refcat=False)
            del matchlist

        except Exception:    
            log.warning("Problem encountered during matching of sources")
            return None
            
    # Check to see whether there were any successful fits...
    align_success = False
    for img in imglist:
        if img.meta['fit_info']['status'] == 'SUCCESS':
            align_success = True
            break
    if align_success:
        # extract results in the style of 'tweakreg'
        resids = extract_residuals(imglist)

        if resids is None:
            resids_files = []
        else:
            resids_files = generate_output_files(resids, 
                                 json_timestamp=None, 
                                 json_time_since_epoch=None, 
                                 exclude_fields=['group_id'])
    else:
        resids_file = []

    return resids_files

def generate_output_files(resids_dict, 
                         json_timestamp=None, 
                         json_time_since_epoch=None, 
                         exclude_fields=['group_id'],
                         calling_name='determine_alignment_residuals'):
    """Write out results to JSON files, one per image"""
    resids_files = []
    for image in resids_dict:
        # Remove any extraneous information from output 
        for field in exclude_fields:
            del resids_dict[image]['fit_results'][field]
        # Define name for output JSON file...
        rootname = image.split("_")[0]
        json_filename = "{}_cal_qa_astrometry_resids.json".format(rootname)
        resids_files.append(json_filename)
        
        # Define output diagnostic object
        diagnostic_obj = du.HapDiagnostic()
        src_str = "{}.{}".format(__taskname__, calling_name) 
        diagnostic_obj.instantiate_from_fitsfile(image,
                                               data_source=src_str,
                                               description="X and Y residuals from \
                                                            relative alignment ",
                                               timestamp=json_timestamp,
                                               time_since_epoch=json_time_since_epoch)
        diagnostic_obj.add_data_item(resids_dict[image]['fit_results'], 'fit_results',
                                     item_description="Fit results for relative alignment of input exposures",
                                     descriptions={"aligned_to":"Reference image for relative alignment",
                                                   "rms_x":"RMS in X for fit",
                                                   "rms_y":"RMS in Y for fit",
                                                   "xsh":"X offset from fit",
                                                   "ysh":"Y offset from fit",
                                                   "rot":"Average Rotation from fit",
                                                   "scale":"Average Scale change from fit",
                                                   "rot_fit":"Rotation of each axis from fit",
                                                   "scale_fit":"Scale of each axis from fit",
                                                   "nmatches":"Number of matched sources used in fit",
                                                   "skew":"Skew between axes from fit"},
                                     units={"aligned_to":"unitless",
                                            'rms_x':'pixels',
                                            'rms_y':'pixels',
                                            'xsh':'pixels',
                                            'ysh':'pixels',
                                            'rot':'degrees',
                                            'scale':'unitless',
                                            'rot_fit':'degrees',
                                            'scale_fit':'unitless',
                                            'nmatches':'unitless',
                                            'skew':'unitless'}
                                     )
        diagnostic_obj.add_data_item(resids_dict[image]['sources'], 'residuals',
                                     item_description="Matched source positions from input exposures",
                                     descriptions={"x":"X position from source image on tangent plane",
                                                   "y":"Y position from source image on tangent plane",
                                                   "ref_x":"X position from ref image on tangent plane",
                                                   "ref_y":"Y position from ref image on tangent plane"},
                                     units={'x':'pixels',
                                            'y':'pixels',
                                            'ref_x':'pixels',
                                            'ref_y':'pixels'}
                                     )

        diagnostic_obj.write_json_file(json_filename)
        log.info("Generated relative astrometri residuals results for {} as {}.".format(image, json_filename))

    return resids_files

def extract_residuals(imglist):
    """Convert fit results and catalogs from tweakwcs into list of residuals"""
    group_dict = {}

    ref_ra, ref_dec = [], []
    for chip in imglist:
        group_id = chip.meta['group_id']
        group_name = chip.meta['filename']
        fitinfo = chip.meta['fit_info']

        if fitinfo['status'] == 'REFERENCE':
            align_ref = group_name
            #group_dict[group_name]['aligned_to'] = 'self'
            rra, rdec = chip.det_to_world(chip.meta['catalog']['x'],
                                          chip.meta['catalog']['y'])
            ref_ra = np.concatenate([ref_ra, rra])
            ref_dec = np.concatenate([ref_dec, rdec])
            continue

        if group_id not in group_dict:
            group_dict[group_name] = {}
            group_dict[group_name]['fit_results'] = {'group_id': group_id,
                         'rms_x': None, 'rms_y': None}
            group_dict[group_name]['sources'] = Table(names=['x', 'y', 
                                                             'ref_x', 'ref_y'])
            cum_indx = 0

        # store results in dict
        group_dict[group_name]['fit_results']['aligned_to'] = align_ref

        if 'fitmask' in fitinfo:
            img_mask = fitinfo['fitmask']
            ref_indx = fitinfo['matched_ref_idx'][img_mask]
            img_indx = fitinfo['matched_input_idx'][img_mask]
            # Extract X, Y for sources image being updated
            img_x, img_y, max_indx, chip_mask = get_tangent_positions(chip, img_indx,
                                                           start_indx=cum_indx)
            cum_indx += max_indx
            
            # Extract X, Y for sources from reference image
            ref_x, ref_y = chip.world_to_tanp(ref_ra[ref_indx][chip_mask], ref_dec[ref_indx][chip_mask])
            group_dict[group_name]['fit_results'].update(
                 {'xsh': fitinfo['shift'][0], 'ysh': fitinfo['shift'][1],
                 'rot': fitinfo['<rot>'], 'scale': fitinfo['<scale>'],
                 'rot_fit': fitinfo['rot'], 'scale_fit': fitinfo['scale'],
                 'nmatches': fitinfo['nmatches'], 'skew': fitinfo['skew'],
                 'rms_x': sigma_clipped_stats((img_x - ref_x))[-1],
                 'rms_y': sigma_clipped_stats((img_y - ref_y))[-1]})

            new_vals = Table(data=[img_x, img_y, ref_x, ref_y], 
                                    names=['x', 'y', 'ref_x', 'ref_y'])
            group_dict[group_name]['sources'] = vstack([group_dict[group_name]['sources'], new_vals])
            
        else: 
            group_dict[group_name]['fit_results'].update(
                     {'xsh': None, 'ysh': None,
                     'rot': None, 'scale': None,
                     'rot_fit': None, 'scale_fit': None,
                     'nmatches': -1, 'skew': None,
                     'rms_x': -1, 'rms_y': -1})


    return group_dict

def get_tangent_positions(chip, indices, start_indx=0):
    img_x = []
    img_y = []
    fitinfo = chip.meta['fit_info']
    img_ra = fitinfo['fit_RA']
    img_dec = fitinfo['fit_DEC']

    # Extract X, Y for sources image being updated
    max_indx = len(chip.meta['catalog'])
    chip_indx = np.where(np.logical_and(indices >= start_indx,
                                        indices < max_indx + start_indx))[0]
    # Get X,Y position in tangent plane where fit was done
    chip_x, chip_y = chip.world_to_tanp(img_ra[chip_indx], img_dec[chip_indx])
    img_x.extend(chip_x)
    img_y.extend(chip_y)

    return img_x, img_y, max_indx, chip_indx


# -------------------------------------------------------------------------------
# Compare source list with GAIA ref catalog
def match_to_gaia(imcat, refcat, product, output, searchrad=5.0):
    """Create a catalog with sources matched to GAIA sources
    
    Parameters
    ----------
    imcat : str or obj
        Filename or astropy.Table of source catalog written out as ECSV file
        
    refcat : str
        Filename of GAIA catalog files written out as ECSV file
        
    product : str
        Filename of drizzled product used to derive the source catalog
        
    output : str
        Rootname for matched catalog file to be written as an ECSV file 
    
    """
    if isinstance(imcat, str):
        imtab = Table.read(imcat, format='ascii.ecsv')
        imtab.rename_column('X-Center', 'x')
        imtab.rename_column('Y-Center', 'y')
    else:
        imtab = imcat
        if 'X-Center' in imtab.colnames:
            imtab.rename_column('X-Center', 'x')
            imtab.rename_column('Y-Center', 'y')
            
    
    reftab = Table.read(refcat, format='ascii.ecsv')
    
    # define WCS for matching
    tpwcs = tweakwcs.FITSWCS(HSTWCS(product, ext=1))
    
    # define matching parameters
    tpmatch = tweakwcs.TPMatch(searchrad=searchrad)
    
    # perform match
    ref_indx, im_indx = tpmatch(reftab, imtab, tpwcs)
    print('Found {} matches'.format(len(ref_indx)))
    
    # Obtain tangent plane positions for both image sources and reference sources
    im_x, im_y = tpwcs.det_to_tanp(imtab['x'][im_indx], imtab['y'][im_indx])
    ref_x, ref_y = tpwcs.world_to_tanp(reftab['RA'][ref_indx], reftab['DEC'][ref_indx])
    if 'RA' not in imtab.colnames:
        im_ra, im_dec = tpwcs.det_to_world(imtab['x'][im_indx], imtab['y'][im_indx])
    else:
        im_ra = imtab['RA'][im_indx]
        im_dec = imtab['DEC'][im_indx]
        

    # Compile match table
    match_tab = Table(data=[im_x, im_y, im_ra, im_dec, 
                            ref_x, ref_y, 
                            reftab['RA'][ref_indx], reftab['DEC'][ref_indx]],
                      names=['img_x','img_y', 'img_RA', 'img_DEC', 
                             'ref_x', 'ref_y', 'ref_RA', 'ref_DEC'])
    if not output.endswith('.ecsv'):
        output = '{}.ecsv'.format(output)                             
    match_tab.write(output, format='ascii.ecsv')
    
                       


# -------------------------------------------------------------------------------
# Simple interface for running all the analysis functions defined for this package
def run_all(input, files, log_level=logutil.logging.NOTSET):

    # generate a timestamp values that will be used to make creation time, creation date and epoch values
    # common to each json file
    json_timestamp = datetime.now().strftime("%m/%d/%YT%H:%M:%S")
    json_time_since_epoch = time.time()

    json_files = determine_alignment_residuals(input, files,
                                             json_timestamp=json_timestamp,
                                             json_time_since_epoch=json_time_since_epoch)
    
    print("Generated quality statistics as {}".format(json_files))


# -------------------------------------------------------------------------------
#  Code for generating relevant plots from these results
def generate_plots(json_data):
    """Create plots from json file or json data"""
    
    if isinstance(json_data, str):
        # Open json file and read in data
        with open(json_data) as jfile:
            json_data = json.load(jfile)
            
    fig_id = 0
    for fname in json_data:
        data = json_data[fname]

        rootname = fname.split("_")[0]
        coldata = [data['x'], data['y'], data['ref_x'], data['ref_y']]
        # Insure all columns are numpy arrays
        coldata = [np.array(c) for c in coldata]
        title_str = 'Residuals\ for\ {0}\ using\ {1:6d}\ sources'.format(
                    fname.replace('_','\_'),data['nmatches'])
        
        vector_name = '{}_vector_quality.png'.format(rootname)
        resids_name = '{}_resids_quality.png'.format(rootname)
        # Generate plots
        tweakutils.make_vector_plot(None, data=coldata,
                     figure_id=fig_id, title=title_str, vector=True,
                     plotname=vector_name)
        fig_id += 1
        tweakutils.make_vector_plot(None, data=coldata, ylimit=0.5,
                     figure_id=fig_id, title=title_str, vector=False,
                     plotname=resids_name)
        fig_id += 1


                     

    
    
    
