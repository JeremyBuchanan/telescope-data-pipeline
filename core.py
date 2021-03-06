import astropy.io.fits as fits
import copy
import numpy as np
from astropy import units as u
from astropy.modeling.fitting import LevMarLSQFitter
from astropy.nddata import NDData, CCDData
from astropy.stats import gaussian_sigma_to_fwhm, sigma_clipped_stats
from astropy.table import Table
from astropy.wcs import WCS
from astroquery.astrometry_net import AstrometryNet
from ccdproc import Combiner
from photutils import aperture_photometry, CircularAperture, EPSFBuilder, CircularAnnulus
from photutils.background import MMMBackground
from photutils.detection import DAOStarFinder, IRAFStarFinder
from photutils.psf import IterativelySubtractedPSFPhotometry, DAOGroup, extract_stars
from scipy.optimize import curve_fit

def import_images(im_list, p):
    '''
    A function that imports the data from an image file, following a given
    path to find the image file

        Paramters
        ---------
        im_list: list
            List containing the names of the image files
        p: string
            The pathway the script should follow to find the image
            files on the computer

        Returns
        -------
        im_data: list
            A list of the data arrays containing the pixel data
            of images
        in_headers: list
            A list of all the image headers
    '''
    im_data = []
    im_headers = []
    for i in im_list:
        x = str(i)
        path = p + x
        hdu = fits.open(path)
        data = hdu[1].data
        header = hdu[1].header
        im_data.append(data)
        im_headers.append(header)

    return im_data, im_headers

def find_fwhm(image, size=100):
    '''
    Fits a 2D gaussian surface to the brightest, non-saturated star
    on an image

        Parameters
        ----------
        image: array-like
            raw pixel data from the image
        size: integer
            radius of the cutout around the star

        Returns
        -------
        popt: list
            list of all the best fit values of the gaussians parameters:
            x0, y0, sig_x, sig_y, Amplitude, offset (background estimate)
    '''
    mean_val, median_val, std_val = sigma_clipped_stats(image, sigma=2.0)
    search_image = image[100:-100,100:-100]
    max_peak = np.max(search_image)
    count = 0
    while max_peak >= 0:
        count += 1
        rs, cs = np.where(search_image==max_peak)[0][0], np.where(search_image==max_peak)[1][0]
        r = rs+100
        c = cs+100
        if max_peak < 50000:
            star = image[r-size:r+size,c-size:c+size]
            x = np.arange(2*size)
            y = np.arange(2*size)
            X, Y = np.meshgrid(x, y)
            def gaussian(M, x0, y0, sig_x, sig_y, A, off):
                x, y = M
                return A * np.exp(-((x-x0)**2)/(2*sig_x**2)-((y-y0)**2)/(2*sig_y**2)) + off
            xdata = np.vstack((X.ravel(), Y.ravel()))
            ydata = star.ravel()
            p = [size, size, 3, 3, 10000, median_val]
            try:
                popt, pcov = curve_fit(f=gaussian, xdata=xdata, ydata=ydata, p0=p)
                im_sig = np.mean(popt[2:4])
                fwhm = im_sig*gaussian_sigma_to_fwhm
            except:
                fwhm = 0
            if fwhm > 2:
                break
            else:
                image[r-size:r+size,c-size:c+size] = 0
                search_image = image[100:-100,100:-100]
                max_peak = np.max(search_image)
        else:
            image[r-size:r+size,c-size:c+size] = 0
            search_image = image[100:-100,100:-100]
            max_peak = np.max(search_image)
        if count > 100:
            fwhm = 0
            im_sig = 0
            break
        if max_peak < 1000:
            fwhm = 0
            im_sig = 0
            break
    return fwhm, im_sig

def find_stars(image, sigma, peak=100000):
    '''
    Searches data from an image to find objects above a given brightness
    threshold based off parameters of the ccd chip

        Parameters
        ----------
        image: array-like
            Array containing the intensity of light for each pixel
            on the ccd chip
        sigma: float
            sets the size tolerance for detected objects. Usually
            5.0, more than 5 is statistically unreasonable
        peak: int
            The max number of counts the chip can handle before the
            image starts to smear. Usual ccd can handle 100,000 counts

        Returns
        -------
        stars: table
            A table containing all the found stars and their parameters:
            id, xcentroid, ycentroid, sharpness, roundness, npix, sky,
            peak, flux, mag
    '''
    sigma_psf = sigma
    mean_val, median_val, std_val = sigma_clipped_stats(image, sigma=2.0)
    bkg = median_val
    daofind = DAOStarFinder(fwhm=sigma_psf*gaussian_sigma_to_fwhm, threshold=bkg+10*std_val,
                            sky=bkg, peakmax=peak, exclude_border=True)
    stars = daofind(image)
    return stars

def calculate_shift(stars1, stars2):
    '''
    Calculates the necessary shift of one image in order to be aligned
    with a second image

        Parameters
        ----------
        stars1: table
            The table returned from using find_stars on an image
        stars2: table
            Same as stars1, for a different image

        Returns
        -------
        diff: table
            Table containing the x, y, and total offset of each star object
            found between two images
    '''
    diff = np.zeros([stars1['xcentroid'].size, 3])*np.nan
    for i in range(stars1['xcentroid'].size):
        dx = stars1['xcentroid'][i] - stars2['xcentroid']
        dy = stars1['ycentroid'][i] - stars2['ycentroid']
        distances = np.abs(np.sqrt((dx)**2 + (dy)**2))
        match = (distances == np.min(distances))
        if distances[match] < 20:
            diff[i, 0] = distances[match]
            diff[i, 1] = dx[match]
            diff[i, 2] = dy[match]

    return diff

def roll_image(image, diff, threshold=0.5):
    '''
    Averages the x and y offset of objects on 2 images to the nearest
    integer, and then rolls the image by that number of pixels along each
    axis. Good for aligning two images

        Parameters
        ----------
        image: array-like
            Array containing the intensity of light for each pixel
            on the ccd chip
        diff: table
            Table containing the x, y, and total offset of each star object
            found between two images
        threshold: float
            The minimum pixel offset between images to allow shifting,
            usually 0.5 pixels

        Returns
        -------
        image_shift: array-like
            The "rolled" version of the same image, now aligned to another
            reference image
    '''
    offset = np.median(diff[:, 0])
    if offset >= threshold:
        xshift = np.median(diff[:, 1])
        yshift = np.median(diff[:, 2])
        xshift_int = np.int(np.round(xshift, 0))
        yshift_int = np.int(np.round(yshift, 0))
        image_shift = np.roll(image, (yshift_int, xshift_int), axis = (0, 1))

        return image_shift
    else:
        return image

def median_combiner(images):
    '''
    Function that takes the median of multiple images containing the
    same stars objects

        Parameters
        ----------
        images: list
            A list of the data arrays containing the pixel data
            of images

        Returns
        -------
        median_image: array-like
            Array containing the median intensity of light for each
            pixel for a set of images
    '''
    ccd_image_list = []

    for image in images:
        ccd_image = CCDData(image, unit=u.adu)
        ccd_image_list.append(ccd_image)

    c = Combiner(ccd_image_list)
    c.sigma_clipping(func = np.ma.median)
    median_image = c.median_combine()
    median_image = np.asarray(median_image)

    return median_image

def image_combiner(im_data, im_sig):
    '''
    Returns a median combination of a list of images

        Parameters
        ----------
        im_data: list
            contains all the image data from the image set
        im_sig: float
            an image customized size parameter for searching an
            image for stars

        Returns
        -------
        median_image: array-like
    '''
    stars = []
    for i in im_data:
        s = find_stars(image=i, sigma=im_sig, peak=100000)
        stars.append(s)
    if s is None:
        median_image = None
        return median_image
    else:
        diffs = []
        for s in range(len(stars)):
                diff = calculate_shift(stars1=stars[0], stars2=stars[s])
                diffs.append(diff)
        images = []
        for i in range(len(im_data)):
            image_shift = roll_image(image=im_data[i], diff=diffs[i], threshold=0.5)
            images.append(image_shift)
        median_image = median_combiner(images=images)

        return median_image

def image_mask(image, sources, fwhm, bkg, bkg_std):
    '''
    Masking routine that rejects stars too close to the edge of the
    image, too close to each other, and the 5 brightest and 5 dimmest
    stars in the image

        Parameters
        ----------
        image: array-like
            raw pixel data from the image
        sources: Table
            contains all the data aquired from the star searching routine
        fwhm: float
            used for scaling the mask based on how focused the image is

        Returns
        -------
        stars_tbl: Table
            condensed form of the sources table, excluding all the masked
            stars. columns: xcentroid, ycentroid, flux, peak, id
    '''
    size = 100
    hsize = (size - 1) / 2
    x = sources['xcentroid']
    y = sources['ycentroid']
    flux = sources['flux']
    i = sources['id']
    p = sources['peak']
    mask = ((x > hsize) & (x < (image.shape[1] - 1 - hsize)) &
            (y > hsize) & (y < (image.shape[0] - 1 - hsize)))
    stars_tbl = Table()
    stars_tbl['x'] = x[mask]
    stars_tbl['y'] = y[mask]
    stars_tbl['flux'] = flux[mask]
    stars_tbl['id'] = i[mask]
    stars_tbl['peak'] = p[mask]
    d = []
    idxi = 0
    for i in stars_tbl['id']:
        idxj = 0
        for j in stars_tbl['id']:
            if i != j:
                threshold = 5*fwhm
                dx = stars_tbl['x'][idxi] - stars_tbl['x'][idxj]
                dy = stars_tbl['y'][idxi] - stars_tbl['y'][idxj]
                distance = np.abs(np.sqrt((dx)**2 + (dy)**2))
                if distance <= threshold:
                    d.append(idxi)
            idxj = idxj+1
        idxi = idxi + 1
    idxp = 0
    min_peak = bkg + 10 * bkg_std
    for i in stars_tbl['peak']:
        if i <= min_peak:
            d.append(idxp)
        idxp += 1
    stars_tbl.remove_rows(d)
    stars_tbl.sort('flux', reverse=True)
    if len(stars_tbl) > 10:
        stars_tbl.remove_rows([-5,-4,-3,-2,-1,0,1,2,3,4])

    return stars_tbl

def bkg_sub(image, stars_tbl, fwhm):
    '''
    Local background subtraction routine for stars on an image

        Parameters
        ----------
        image: array-like
            raw pixel data of the image
        stars_tbl: Table
            contains positional and flux data for all the stars
        fwhm: float
            used for scaling the area to be background subtracted
            based on how focused the image is

        Returns
        -------
        image_lbs: array-like
            a copy of the original image, with regions around each star
            containing no background flux
    '''
    image_lbs = copy.deepcopy(image)
    for s in stars_tbl['x','y']:
        position = [s[0],s[1]]
        aperture = CircularAperture(position, r=20)
        annulus = CircularAnnulus(position, r_in=20, r_out=30)
        annulus_mask = annulus.to_mask(method='center')
        annulus_data = annulus_mask.multiply(image_lbs)
        annulus_data_1d = annulus_data[annulus_mask.data > 0]
        _, median_sigclip, _ = sigma_clipped_stats(annulus_data_1d)
        bkg_median = median_sigclip
        pos_pix = [np.int(np.round(position[0], 0)), np.int(np.round(position[1], 0))]
        size = 5*fwhm
        for r in range(len(image_lbs)):
            if (r > pos_pix[1]-(size/2) and r < pos_pix[1]+(size/2)):
                for c in range(len(image_lbs[r])):
                    if (c > pos_pix[0]-(size/2) and c < pos_pix[0]+(size/2)):
                        image_lbs[r][c] -= bkg_median

    return image_lbs

def build_psf(image, stars_tbl, fwhm):
    '''
    Constructs a poins spread function (psf) from a sample of stars
    on an image

        Parameters
        ----------
        image: array-like
            raw pixel data of the image
        stars_tbl: Table
            contains positional and flux data for all the stars
        fwhm: float
            used for scaling the size of the star cutouts based on
            how focused the image is

        Returns
        -------
        epsf: EPSFModel
            the effective psf constructed form the stars
        stars: EPSFStars
            the star cutouts used to build the psf
        fitted_stars: EPSFStars
            the original stars, with updated centers and fluxes derived
            from fitting the output psf
    '''
    nddata = NDData(data = image)
    stars = extract_stars(nddata, stars_tbl, size = 5*fwhm)
    epsf_builder = EPSFBuilder(oversampling=2, maxiters=10, progress_bar=False, smoothing_kernel='quadratic')
    epsf, fitted_stars = epsf_builder(stars)

    return epsf, stars, fitted_stars

def do_photometry(image, epsf, fwhm):
    '''
    Iterative photometry routine using a point spread function (psf) model

        Parameters
        ----------
        image: array-like
            raw pixel data from the image
        epsf: EPSFModel
            the psf model for finding stars on the image
        fwhm: float
            used for scaling data collection region around each star based
            on how focused the image is

        Returns
        -------
        results: Table
            contains all the photometry data: x_0, x_fit, y_0, y_fit, flux_0,
            flux_fit, id,group_id, flux_unc, x_0_unc, y_0_unc, iter_detected
        photometry:
            the iterative search function for performing photometry
    '''
    mean_val, median_val, std_val = sigma_clipped_stats(image, sigma=2.0)
    daofind = DAOStarFinder(fwhm=fwhm, threshold=median_val+20*std_val, sky=median_val, peakmax=100000, exclude_border=True)
    daogroup = DAOGroup(2*fwhm)
    mmm_bkg = MMMBackground()
    fitter = LevMarLSQFitter()
    def round_to_odd(f):
        return np.ceil(f) // 2 * 2 + 1
    size = 5*fwhm
    fitshape = np.int(round_to_odd(size))
    photometry = IterativelySubtractedPSFPhotometry(finder=daofind, group_maker=daogroup, bkg_estimator=mmm_bkg,
                                                    psf_model=epsf, fitter=fitter, niters=5, fitshape=fitshape,
                                                    aperture_radius=(size-1)/2)
    results = photometry(image)

    return results, photometry

def get_residuals(results, photometry, fwhm, image):
    '''
    Generates residual image cutouts from photometry results

        Parameters
        ----------
        results: Table
            contains all the photometry data: x_0, x_fit, y_0, y_fit, flux_0,
            flux_fit, id,group_id, flux_unc, x_0_unc, y_0_unc, iter_detected
        photometry:
            the iterative search function for performing photometry

        Results
        -------
        results_tbl: Table
            condensed table of the photometry results, with just the positional
            and flux data
        residual_stars: EPSFStars
            cutouts of the residuals of the stars left after photometry is completed
    '''
    results_tbl = Table()
    results_tbl['x'] = results['x_fit']
    results_tbl['y'] = results['y_fit']
    results_tbl['flux'] = results['flux_fit']
    results_tbl.sort('flux', reverse=True)
    ndresidual = NDData(data=photometry.get_residual_image())
    nddata = NDData(data=image)
    final_stars = extract_stars(nddata, results_tbl, size=5*fwhm)
    residual_stars = extract_stars(ndresidual, results_tbl, size=5*fwhm)

    return results_tbl, final_stars, residual_stars

def get_wcs(results_tbl):
    '''
    Queries the website astrometry.net with image data, returning a world coordinate
    system (wcs) solution, along with a header containing this solution

        Parameters
        ----------
        results_tbl: Table
            contains positional and flux data for all stars found in the photometry
            routine

        Results
        -------
        sky: Table
            contains all locations for the stars in results_tbl in RA and DEC
            instead of pixels
        wcs_header: Header
            an image header with the RA and DEC included
    '''
    ast = AstrometryNet()
    ast.api_key = 'XXXXXXXX'
    try_again = True
    submission_id = None
    image_width = 4096
    image_height = 4096
    while try_again:
        try:
            if not submission_id:
                wcs_header = ast.solve_from_source_list(results_tbl['x'][:30], results_tbl['y'][:30],
                                                        image_width, image_height, submission_id=submission_id,
                                                        solve_timeout=600)
            else:
                wcs_header = ast.monitor_submission(submission_id, solve_timeout=600)
        except TimeoutError as e:
            submission_id = e.args[1]
        else:
            try_again = False

    if wcs_header:
        w = WCS(wcs_header)
        sky = w.pixel_to_world(results_tbl['x'], results_tbl['y'])
        return sky, wcs_header
    else:
        return None, wcs_header
