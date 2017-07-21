#!/usr/bin/python
"""
Script to simulate time-ordered data generated by a CMB experiment
scanning the sky.

Author: Julien Peloton, j.peloton@sussex.ac.uk
"""
from __future__ import division, absolute_import, print_function

import sys
import os

import numpy as np
import healpy as hp
import cPickle as pickle

from s4cmb.detector_pointing import Pointing
from s4cmb.detector_pointing import radec2thetaphi
from s4cmb import input_sky
from s4cmb.tod_f import tod_f
from s4cmb.xpure import qu_weight_mineig

d2r = np.pi / 180.0
am2rad = np.pi / 180. / 60.

class TimeOrderedDataPairDiff():
    """ Class to handle Time-Ordered Data (TOD) """
    def __init__(self, hardware, scanning_strategy, HealpixFitsMap,
                 CESnumber, projection='healpix',
                 nside_out=None, pixel_size=None, width=20.):
        """
        C'est parti!

        Parameters
        ----------
        hardware : Hardware instance
            Instance of Hardware containing instrument parameters and models.
        scanning_strategy : ScanningStrategy instance
            Instance of ScanningStrategy containing scan parameters.
        HealpixFitsMap : HealpixFitsMap instance
            Instance of HealpixFitsMap containing input sky parameters.
        CESnumber : int
            Number of the scan to simulate. Must be between 0 and
            scanning_strategy.nces - 1.
        projection : string, optional
            Type of projection for the output map. Currently available:
            healpix, flat. Here is a warning: Because of projection artifact,
            if you choose flat projection, then we will scan the sky *as if
            it was centered in [0., 0.]*. Therefore, one cannot for the moment
            compared directly healpix and flat runs.
        nside_out : int, optional
            The resolution for the output maps if projection=healpix.
            Default is nside of the input map.
        pixel_size : float, optional
            The pixel size for the output maps if projection=flat.
            In arcmin. Default is resolution of the input map.
        width : float, optional
            Width for the output map in degree.
        """
        ## Initialise args
        self.hardware = hardware
        self.scanning_strategy = scanning_strategy
        self.HealpixFitsMap = HealpixFitsMap
        self.width = width
        self.projection = projection
        assert self.projection in ['healpix', 'flat'], \
            ValueError("Projection <{}> for ".format(self.projection) +
                       "the output map not understood! " +
                       "Choose among ['healpix', 'flat'].")

        self.CESnumber = CESnumber
        assert self.CESnumber < self.scanning_strategy.nces, \
            ValueError("The scan index must be between 0 and {}.".format(
                self.scanning_strategy.nces - 1
            ))

        ## Initialise internal parameters
        self.scan = getattr(self.scanning_strategy, 'scan{}'.format(
            self.CESnumber))
        self.nsamples = self.scan['nts']
        self.npair = self.hardware.focal_plane.npair
        self.pair_list = np.reshape(
            self.hardware.focal_plane.bolo_index_in_fp, (self.npair, 2))

        ## Pre-compute boresight pointing objects
        self.get_boresightpointing()

        ## Polarisation angles: intrinsic and HWP angles
        self.get_angles()

        ## Position of bolometers in the focal plane
        ## TODO move that elsewhere...
        self.ypos = self.hardware.beam_model.ypos
        self.xpos = self.hardware.beam_model.xpos
        self.xpos = self.xpos / np.cos(self.ypos)

        ## Initialise pointing matrix, that is the matrix to go from time
        ## to map domain, for all pairs of detectors.
        self.point_matrix = np.zeros(
            (self.npair, self.nsamples), dtype=np.int32)

        ## Initialise the mask for timestreams
        self.wafermask_pixel = self.get_timestream_masks()

        ## Get observed pixels in the input map
        if nside_out is None:
            self.nside_out = self.HealpixFitsMap.nside
        else:
            self.nside_out = nside_out
        if pixel_size is None:
            self.pixel_size = hp.nside2resol(self.HealpixFitsMap.nside)
        else:
            self.pixel_size = pixel_size * am2rad

        self.obspix, self.npixsky = self.get_obspix(
            self.width,
            self.scanning_strategy.ra_mid,
            self.scanning_strategy.dec_mid)

        ## Get timestream weights
        self.sum_weight, self.diff_weight = self.get_weights()

    def get_angles(self):
        """
        Retrieve polarisation angles: intrinsic (focal plane) and HWP angles,
        and initialise total polarisation angle.
        """
        self.hwpangle = self.hardware.half_wave_plate.compute_HWP_angles(
            sample_rate=self.scan['sample_rate'],
            size=self.nsamples)

        self.intrinsic_polangle = self.hardware.focal_plane.bolo_polangle

        ## Will contain the total polarisation angles for all bolometers
        ## That is PA + intrinsic + 2 * HWP
        self.pol_angs = np.zeros((self.npair, self.nsamples))

    def get_timestream_masks(self):
        """
        Define the masks for all the timestreams.
        1 if the time sample should be included, 0 otherwise.
        Set to ones for the moment.
        """
        return np.ones((self.npair, self.nsamples), dtype=int)

    def get_obspix(self, width, ra_src, dec_src):
        """
        Return the index of observed pixels within a given patch
        defined by (`ra_src`, `dec_src`) and `width`.
        This will be the sky patch that will be returned.

        Parameters
        ----------
        width : float
            Width of the patch in degree.
        ra_src : float
            RA of the center of the patch in degree.
        dec_src : float
            Dec of the center of the patch in degree.

        Returns
        ----------
        obspix : 1d array
            The indices of the observed pixels in the input map. Same for
            all output projection as the only input projection is healpix.
        nskypix : int
            Number of sky pixels in our patch given the boundaries (specified
            via `width`). For healpix projection, this is the length of the
            obspix. For flat projection, this is the number of square pixels
            within the patch.

        Examples
        ----------
        Healpix projection
        >>> inst, scan, sky_in = load_fake_instrument()
        >>> tod = TimeOrderedDataPairDiff(inst, scan, sky_in, CESnumber=0)
        >>> obspix, npix = tod.get_obspix(10., 0., 0.)
        >>> print(obspix)
        [1376 1439 1440 1504 1567 1568 1632 1695]

        Flat projection
        >>> inst, scan, sky_in = load_fake_instrument()
        >>> tod = TimeOrderedDataPairDiff(inst, scan, sky_in, CESnumber=0,
        ...     projection='flat')
        >>> obspix, npix = tod.get_obspix(10., 0., 0.)
        >>> print(obspix) ## the same as healpix
        [1376 1439 1440 1504 1567 1568 1632 1695]
        >>> print(npix, len(obspix))
        16 8
        """
        ## Change to radian
        ra_src = ra_src * d2r
        dec_src = dec_src * d2r
        ## TODO implement the first line correctly...
        try:
            xmin, xmax, ymin, ymax = np.array(width) * d2r
        except TypeError:
            xmin = xmax = ymin = ymax = d2r * width / 2.

        # If map bound crosses zero make coordinates
        ## bounds monotonic in [-pi,pi]
        ra_min = (ra_src - xmin)
        if (ra_src + xmax) >= 2 * np.pi:
            ra_max = (ra_src + xmax) % (2 * np.pi)
            ra_min = ra_min if ra_min <= np.pi else ra_min - 2 * np.pi
        else:
            ra_min = (ra_src - xmin)
            ra_max = (ra_src + xmax)

        dec_min = max([(dec_src - ymin), -np.pi/2])
        dec_max = min([dec_src + ymax, np.pi/2])

        self.xmin = ra_min
        self.xmax = ra_max
        self.ymin = dec_min
        self.ymax = dec_max

        obspix = input_sky.get_obspix(ra_min, ra_max,
                                      dec_min, dec_max,
                                      self.nside_out)

        if self.projection == 'flat':
            npixsky = int(
                round(
                    (self.xmax - self.xmin + self.pixel_size) /
                    self.pixel_size))**2
        elif self.projection == 'healpix':
            npixsky = len(obspix)

        return obspix, npixsky

    def get_weights(self):
        """
        Return the noise weights of the sum and difference timestreams
        (in 1/noise units).
        For the moment, there is one number per pair for the whole scan.
        Typically, this can be the (mean) PSD of the timestream.

        Default for the moment is 1 (i.e. no weights).

        Returns
        ----------
        sum_weight : 1d array
            Weights for the sum of timestreams (size: npair)
        diff_weight : 1d array
            Weights for the difference of timestreams (size: npair)
        """
        return np.ones((2, self.npair), dtype=int)

    def get_boresightpointing(self):
        """
        Initialise the boresight pointing for all the focal plane bolometers.
        The actual pointing (RA/Dec/Parallactic angle) is computed on-the-fly
        when we load the data.

        Note:
        For healpix projection, our (ra_src, dec_src) = (0, 0) and we
        rotate the input map while for flat we true center of the patch.
        This is to avoid projection artifact by operating a rotation
        of the coordinates to (0, 0) in flat projection (scan around equator).
        """
        lat = float(
            self.scanning_strategy.telescope_location.lat) * 180. / np.pi

        if self.projection == 'healpix':
            ra_src = 0.0
            dec_src = 0.0
        elif self.projection == 'flat':
            ra_src = self.scanning_strategy.ra_mid
            dec_src = self.scanning_strategy.dec_mid * np.pi / 180.

            ## Perform a rotation of the input to put the point
            ## (ra_src, dec_src) at (0, 0).
            r = hp.Rotator(rot=[ra_src, self.scanning_strategy.dec_mid])
            theta, phi = hp.pix2ang(self.HealpixFitsMap.nside,
                                    range(12 * self.HealpixFitsMap.nside**2))
            t, p = r(theta, phi, inv=True)
            pix = hp.ang2pix(self.HealpixFitsMap.nside, t, p)

            ## Apply the rotation to our maps
            self.HealpixFitsMap.I = self.HealpixFitsMap.I[pix]
            self.HealpixFitsMap.Q = self.HealpixFitsMap.Q[pix]
            self.HealpixFitsMap.U = self.HealpixFitsMap.U[pix]

        self.pointing = Pointing(
            az_enc=self.scan['azimuth'],
            el_enc=self.scan['elevation'],
            time=self.scan['clock-utc'],
            value_params=self.hardware.pointing_model.value_params,
            allowed_params=self.hardware.pointing_model.allowed_params,
            ut1utc_fn=self.scanning_strategy.ut1utc_fn,
            lat=lat, ra_src=ra_src, dec_src=dec_src)

    def compute_simpolangle(self, ch, parallactic_angle, do_demodulation=False,
                            polangle_err=False):
        """
        Compute the full polarisation angles used to generate timestreams.
        The polarisation angle contains intrinsic polarisation angle (from
        focal plane design), parallactic angle (from pointing), and the angle
        from the half-wave plate.

        Parameters
        ----------
        ch : int
            Channel index in the focal plane.
        parallactic_angle : 1d array
            All parallactic angles for detector ch.
        do_demodulation : bool, optional
            If True, use the convention for the demodulation (extra minus sign)
        polangle_err : bool, optional
            If True, inject systematic effect.
            TODO: remove that in the systematic module.

        Returns
        ----------
        pol_ang : 1d array
            Vector containing the values of the polarisation angle for the
            whole scan.

        Examples
        ----------
        >>> inst, scan, sky_in = load_fake_instrument()
        >>> tod = TimeOrderedDataPairDiff(inst, scan, sky_in, CESnumber=0)
        >>> angles = tod.compute_simpolangle(ch=0,
        ...     parallactic_angle=np.array([np.pi] * tod.nsamples))
        >>> print(angles[:4])
        [  0.          25.13274123  50.26548246  75.39822369]
        """
        if not polangle_err:
            ang_pix = (90.0 - self.intrinsic_polangle[ch]) * d2r
            if not do_demodulation:
                pol_ang = parallactic_angle + ang_pix + 2.0 * self.hwpangle
            else:
                pol_ang = parallactic_angle - ang_pix - 2.0 * self.hwpangle
        else:
            print("This is where you call the systematic module!")
            sys.exit()
            pass

        return pol_ang

    def map2tod(self, ch):
        """
        Scan the input sky maps to generate timestream for channel ch.
        /!\ this is currently the bottleneck in computation. Need to speed
        up this routine!

        Parameters
        ----------
        ch : int
            Channel index in the focal plane.

        Returns
        ----------
        ts : 1d array
            The timestream for detector ch. If `self.HealpixFitsMap.do_pol` is
            True it returns intensity+polarisation, otherwise just intensity.

        Examples
        ----------
        >>> inst, scan, sky_in = load_fake_instrument()
        >>> tod = TimeOrderedDataPairDiff(inst, scan, sky_in, CESnumber=1)
        >>> d = tod.map2tod(0)
        >>> print(round(d[0], 3)) #doctest: +NORMALIZE_WHITESPACE
        -42.874
        """
        ## Use bolometer beam offsets.
        azd, eld = self.xpos[ch], self.ypos[ch]

        ## Compute pointing for detector ch
        ra, dec, pa = self.pointing.offset_detector(azd, eld)

        ## Retrieve corresponding pixels on the sky, and their index locally.
        if self.projection == 'flat':
            ##
            index_global, index_local = build_pointing_matrix(
                ra, dec, self.HealpixFitsMap.nside,
                xmin=-self.width/2.*np.pi/180.,
                ymin=-self.width/2.*np.pi/180.,
                pixel_size=self.pixel_size,
                npix_per_row=int(np.sqrt(self.npixsky)),
                projection=self.projection)
        elif self.projection == 'healpix':
            index_global, index_local = build_pointing_matrix(
                ra, dec, self.HealpixFitsMap.nside, obspix=self.obspix,
                cut_outliers=True, ext_map_gal=self.HealpixFitsMap.ext_map_gal,
                projection=self.projection)

        ## Store list of hit pixels only for top bolometers
        if ch % 2 == 0:
            self.point_matrix[int(ch/2)] = index_local

        ## Gain mode. Not yet implemented, but this is the place!
        norm = 1.0

        if self.HealpixFitsMap.do_pol:
            pol_ang = self.compute_simpolangle(ch, pa,
                                               do_demodulation=False,
                                               polangle_err=False)

            ## Store list polangle only for top bolometers
            if ch % 2 == 0:
                self.pol_angs[int(ch/2)] = pol_ang

            return (self.HealpixFitsMap.I[index_global] +
                    self.HealpixFitsMap.Q[index_global] * np.cos(2 * pol_ang) +
                    self.HealpixFitsMap.U[index_global] *
                    np.sin(2 * pol_ang)) * norm
        else:
            return norm * self.HealpixFitsMap.I[index_global]

    def tod2map(self, waferts, output_maps):
        """
        Project time-ordered data into sky maps for the whole array.
        Maps are updated on-the-fly. Massive speed-up thanks to the
        interface with fortran. Memory consuming though...

        Parameters
        ----------
        waferts : ndarray
            Array of timestreams. Size (ndetectors, ntimesamples).
        output_maps : OutputSkyMap instance
            Instance of OutputSkyMap which contains the sky maps. The
            coaddition of data is done on-the-fly directly.

        Examples
        ----------
        HEALPIX: Test the routines MAP -> TOD -> MAP.
        >>> inst, scan, sky_in = load_fake_instrument()
        >>> tod = TimeOrderedDataPairDiff(inst, scan, sky_in,
        ...     CESnumber=0, projection='healpix')
        >>> d = np.array([tod.map2tod(det) for det in range(2 * tod.npair)])
        >>> m = OutputSkyMap(projection=tod.projection,
        ...     nside=tod.nside_out, obspix=tod.obspix)
        >>> tod.tod2map(d, m)

        Check intensity map
        >>> sky_out = np.zeros(12 * tod.nside_out**2)
        >>> sky_out[tod.obspix] = m.get_I()
        >>> mask = sky_out != 0.0
        >>> assert np.allclose(sky_out[mask], sky_in.I[mask])

        Check polarisation maps
        >>> sky_out = np.zeros((2, 12 * tod.nside_out**2))
        >>> sky_out[0][tod.obspix], sky_out[1][tod.obspix] = m.get_QU()
        >>> mask = (sky_out[0] != 0.0) * (sky_out[1] != 0.0)
        >>> assert np.allclose(sky_out[0][mask], sky_in.Q[mask])
        >>> assert np.allclose(sky_out[1][mask], sky_in.U[mask])

        FLAT: Test the routines MAP -> TOD -> MAP.
        >>> inst, scan, sky_in = load_fake_instrument()
        >>> tod = TimeOrderedDataPairDiff(inst, scan, sky_in,
        ...     CESnumber=0, projection='flat')
        >>> d = np.array([tod.map2tod(det) for det in range(2 * tod.npair)])
        >>> m = OutputSkyMap(projection=tod.projection,
        ...     npixsky=tod.npixsky, pixel_size=tod.pixel_size)
        >>> tod.tod2map(d, m)

        Check we are looking in the right direction, i.e. mean of the maps
        are within 1% (due to different projection, input and output cannot
        agree completely).
        >>> flat = m.get_I()
        >>> nx = int(np.sqrt(m.npixsky))
        >>> curve = hp.gnomview(tod.HealpixFitsMap.I, rot=[0., 0.],
        ...     xsize=nx, ysize=nx, reso=m.pixel_size,
        ...     return_projected_map=True, flip='geo').flatten()
        >>> mask = flat != 0
        >>> mean_output = np.mean(flat[mask])
        >>> mean_input = np.mean(curve[mask])
        >>> assert (mean_output - mean_input)/mean_output * 100 < 1.0

        """
        npair = waferts.shape[0]
        npixfp = npair / 2
        nt = int(waferts.shape[1])

        ## Check sizes
        assert npixfp == self.point_matrix.shape[0]
        assert nt == self.point_matrix.shape[1]

        assert npixfp == self.pol_angs.shape[0]
        assert nt == self.pol_angs.shape[1]

        assert npixfp == self.diff_weight.shape[0]
        assert npixfp == self.sum_weight.shape[0]

        point_matrix = self.point_matrix.flatten()
        pol_angs = self.pol_angs.flatten()
        waferts = waferts.flatten()
        diff_weight = self.diff_weight.flatten()
        sum_weight = self.sum_weight.flatten()
        wafermask_pixel = self.wafermask_pixel.flatten()

        tod_f.tod2map_alldet_f(output_maps.d, output_maps.w, output_maps.dc,
                               output_maps.ds, output_maps.cc, output_maps.cs,
                               output_maps.ss, output_maps.nhit,
                               point_matrix, pol_angs, waferts,
                               diff_weight, sum_weight, nt,
                               wafermask_pixel, npixfp, self.npixsky)
        # Garbage collector guard
        wafermask_pixel

class OutputSkyMap():
    """ Class to handle sky maps generated by tod2map """
    def __init__(self, projection,
                 obspix=None, npixsky=None, nside=None, pixel_size=None):
        """
        Initialise all maps: weights, projected TOD, and Stokes parameters.

        Parameters
        ----------
        projection : string
            Type of projection among [healpix, flat].
        obspix : 1d array, optional
            List of indices of observed pixels if projection=healpix. No effect
            if projection=flat.
        npixsky : int, optional
            The number of observed sky pixels in projection=flat.
            npixsky is by default len(obspix) if projection=healpix.
        nside : int, optional
            The resolution for the output map if projection=healpix. No effect
            if projection=flat.
        pixel_size : float, optional
            The size of pixels in arcmin if projection=flat. No effect
            if projection=healpix.
        """
        self.nside = nside
        self.projection = projection
        self.obspix = obspix
        self.npixsky = npixsky
        self.nside = nside
        self.pixel_size = pixel_size

        if self.projection == 'healpix':
            assert self.obspix is not None, \
                ValueError("You need to provide the list (obspix) " +
                           "of observed pixel if projection=healpix!")
            assert self.nside is not None, \
                ValueError("You need to provide the resolution (nside) " +
                           "of the map if projection=healpix!")
            self.npixsky = len(self.obspix)
            self.pixel_size = hp.nside2resol(nside, arcmin=True)

        elif self.projection == 'flat':
            assert self.npixsky is not None, \
                ValueError("You need to provide the number of " +
                           "observed pixels (npixsky) if projection=flat.")
            assert self.pixel_size is not None, \
                ValueError("You need to provide the size of " +
                           "pixels (pixel_size) in arcmin if projection=flat.")

        self.initialise_sky_maps()

    def initialise_sky_maps(self):
        """
        Create empty sky maps. This includes:
        * d : projected weighted sum of timestreams
        * dc : projected noise weighted difference of timestreams
            multiplied by cosine
        * ds : projected noise weighted difference of timestreams
            multiplied by sine
        * nhit : projected hit counts.
        * w : projected (inverse) noise weights and hits.
        * cc : projected noise weighted cosine**2
        * ss : projected noise weighted sine**2
        * cs : projected noise weighted cosine * sine.

        """
        # To accumulate A^T N^-1 d
        self.d = np.zeros(self.npixsky)
        self.dc = np.zeros(self.npixsky)
        self.ds = np.zeros(self.npixsky)

        # To accumulate A^T N^-1 A
        self.w = np.zeros(self.npixsky)
        self.cc = np.zeros(self.npixsky)
        self.cs = np.zeros(self.npixsky)
        self.ss = np.zeros(self.npixsky)

        self.nhit = np.zeros(self.npixsky, dtype=np.int32)

    def get_I(self):
        """
        Solve for the intensity map I from projected sum timestream map d
        and weights w: w * I = d.

        Returns
        ----------
        I : 1d array
            Intensity map. Note that only the observed pixels defined in
            obspix are returned (and not the full sky map).
        """
        hit = self.w > 0
        I = np.zeros_like(self.d)
        I[hit] = self.d[hit]/self.w[hit]
        return I

    def get_QU(self):
        """
        Solve for the polarisation maps from projected difference timestream
        maps and weights:

        [cc cs]   [Q]   [dc]
        [cs ss] * [U] = [ds]

        Returns
        ----------
        Q : 1d array
            Stokes Q map. Note that only the observed pixels defined in
            obspix are returned (and not the full sky map).
        U : 1d array
            Stokes U map. Note that only the observed pixels defined in
            obspix are returned (and not the full sky map).
        """
        testcc = self.cc * self.ss - self.cs * self.cs
        idet = np.zeros(testcc.shape)
        inonzero = (testcc != 0.)
        idet[inonzero] = 1. / testcc[inonzero]

        thresh = np.finfo(np.float32).eps
        try:
            izero = (np.abs(testcc) < thresh)
        except FloatingPointError:
            izero = inan = np.isnan(testcc)
            izero[~inan] = (np.abs(testcc[~inan]) < thresh)

        idet[izero] = 0.0
        self.idet = idet

        Q = idet * (self.ss * self.dc - self.cs * self.ds)
        U = idet * (-self.cs * self.dc + self.cc * self.ds)

        return Q, U

    def get_IQU(self):
        """
        Solve for the temperature and polarisation maps from
        projected sum and difference timestream maps and weights:

        [w 0  0 ]   [I]   [d ]
        [0 cc cs]   [Q]   [dc]
        [0 cs ss] * [U] = [ds]

        Returns
        ----------
        I : 1d array
            Intensity map. Note that only the observed pixels defined in
            obspix are returned (and not the full sky map).
        Q : 1d array
            Stokes Q map. Note that only the observed pixels defined in
            obspix are returned (and not the full sky map).
        U : 1d array
            Stokes U map. Note that only the observed pixels defined in
            obspix are returned (and not the full sky map).
        """
        I = self.get_I()
        Q, U = self.get_QU()
        return I, Q, U

    def coadd(self, other, to_coadd='d dc ds w cc cs ss nhit'):
        """
        Add other\'s vectors into our vectors.

        Note:
        You do not need this routine most of the case as
        tod2map operates a coaddition internally if you pass the
        same OutputSkyMap instance.

        Parameters
        ----------
        other : OutputSkyMap instance
            Instance of OutputSkyMap to be coadded with this one.
        to_coadd : string, optional
            String with names of vectors to coadd separated by a space.
            Names must be attributes of other and self.

        Examples
        ---------
        Coadd two maps together.
        >>> m1 = OutputSkyMap(projection='healpix',
        ...     nside=16, obspix=np.array([0, 1, 2, 3]))
        >>> m1.nhit = np.ones(4)
        >>> m2 = OutputSkyMap(projection='healpix',
        ...     nside=16, obspix=np.array([0, 1, 2, 3]))
        >>> m2.nhit = np.ones(4)
        >>> m1.coadd(m2)
        >>> print(m1.nhit)
        [ 2.  2.  2.  2.]

        Same idea in flat sky.
        >>> m1 = OutputSkyMap(projection='flat',
        ...     npixsky=4, pixel_size=2.)
        >>> m1.nhit = np.ones(4)
        >>> m2 = OutputSkyMap(projection='flat',
        ...     npixsky=4, pixel_size=2.)
        >>> m2.nhit = np.ones(4)
        >>> m1.coadd(m2)
        >>> print(m1.nhit)
        [ 2.  2.  2.  2.]
        """
        assert np.all(self.obspix == other.obspix), \
            ValueError("To add maps together, they must have the same obspix!")

        to_coadd_split = to_coadd.split(' ')
        for k in to_coadd_split:
            a = getattr(self, k)
            b = getattr(other, k)
            a += b

    def coadd_MPI(self, other, MPI, to_coadd='d dc ds w cc cs ss nhit'):
        """
        Crappy way of coadding vectors through different processors.

        Parameters
        ----------
        other : OutputSkyMap instance
            Instance of OutputSkyMap to be coadded with this one.
        MPI : module
            Module for communication. It has been tested through mpi4py only
            for the moment.
        to_coadd : string, optional
            String with names of vectors to coadd separated by a space.
            Names must be attributes of other and self.

        Examples
        ---------
        Coadd maps from different processors together.
        >>> from mpi4py import MPI
        >>> m = OutputSkyMap(projection='healpix',
        ...     nside=16, obspix=np.array([0, 1, 2, 3]))
        >>> ## do whatever you want with the maps
        >>> m.coadd_MPI(m, MPI)
        """
        to_coadd_split = to_coadd.split(' ')
        for k in to_coadd_split:
            setattr(self, k, MPI.COMM_WORLD.allreduce(
                getattr(other, k), op=MPI.SUM))

    def pickle_me(self, fn, epsilon=0., verbose=False):
        """
        Save data into pickle file.

        Parameters
        ----------
        fn: string
            The name of the file where data will be stored.
        epsilon : float, optional
            Threshold for selecting the pixels in polarisation.
            0 <= epsilon < 1/4. The higher the more selective.

        """
        I, Q, U = self.get_IQU()
        wP = qu_weight_mineig(self.cc, self.cs, self.ss,
                              epsilon=epsilon, verbose=verbose)

        data = {'I': I, 'Q': Q, 'U': U,
                'wI': self.w, 'wP': wP, 'nhit': self.nhit,
                'projection': self.projection,
                'nside': self.nside, 'pixel_size': self.pixel_size,
                'obspix': self.obspix}

        with open(fn, 'wb') as f:
            pickle.dump(data, f, protocol=2)


def partial2full(partial_obs, obspix, nside, fill_with=0.0):
    """
    Reconstruct full sky map from a partial observation and a list of observed
    pixels.

    Parameters
    ----------
    partial_obs : 1d array
        Array containg the values of observed pixels.
    obspix : 1d array
        Array containing the healpix indices of observed pixels.
    nside : int
        The resolution of the map (obspix and partial_obs should have the same
        nside).
    fill_with : optional
        Fill the initial array with `fill_with`. Default is 0.0.

    Returns
    ----------
    fullsky : 1d array
        Full sky map of size 12 * nside**2.

    Examples
    ----------
    >>> nside = 16
    >>> data = np.random.rand(10)
    >>> obspix = np.arange(12 * nside**2, dtype=int)[30:40]
    >>> fullsky = partial2full(data, obspix, nside)
    """
    fullsky = np.zeros(12 * nside**2) * fill_with
    fullsky[obspix] = partial_obs
    return fullsky

def build_pointing_matrix(ra, dec, nside, projection='healpix',
                          obspix=None, ext_map_gal=False,
                          xmin=None, ymin=None,
                          pixel_size=None, npix_per_row=None,
                          cut_outliers=True):
    """
    Given pointing coordinates (RA/Dec), retrieve the corresponding healpix
    pixel index for a full sky map. This acts effectively as an operator
    to go from time domain to map domain.

    If a list of observed pixel in a sky patch provided (obspix),
    the routines returns also local indices of pixels relative
    to where they are in obspix.
    Note that the indexing is done relatively to the sky patch defined by
    (width, ra_src, dec_src). So if for some reason your scanning strategy
    goes outside the defined sky patch, the routine will assign -1 to the
    pixel index (or crash if cut_outliers is False).

    Long story short: make sure that (width, ra_src, dec_src) returns a
    sky patch bigger than what has been defined in the scanning strategy, or
    you will have truncated output sky maps.

    Parameters
    ----------
    ra : float or 1d array
        RA coordinates of the detector in radian.
    dec : float or 1d array
        Dec coordinates of the detector in radian.
    nside : int
        Resolution for the output map.
    obspix : 1d array, optional
        Array with indices of observed pixels for the sky patch (used to make
        the conversion global indices to local indices).
        Should have been built with nside. Default is None.
    cut_outliers : bool, optional
        If True assign -1 to pixels not in obspix. If False, the routine
        crashes if there are pixels outside. No effet if obspix
        is not provided. Default is True.
    ext_map_gal : bool, optional
        If True, perform a rotation of the RA/Dec coordinate to Galactic
        coordinates prior to compute healpix indices. Defaut is False.

    Returns
    ----------
    index_global : float or 1d array
        The indices of pixels for a full sky healpix map.
    index_local : None or float or 1d array
        The indices of pixels relative to where they are in obspix. None if
        obspix is not provided.

    Examples
    ----------
    >>> index_global, index_local = build_pointing_matrix(0.0, -np.pi/4, 16)
    >>> print(index_global)
    2592

    >>> index_global, index_local = build_pointing_matrix(
    ... np.array([0.0, 0.0]), np.array([-np.pi/4, np.pi/4]),
    ...  nside=16, obspix=np.array([0, 1200, 2592]))
    >>> print(index_global, index_local)
    [2592  420] [ 2 -1]
    """
    theta, phi = radec2thetaphi(ra, dec)
    if ext_map_gal:
        r = hp.Rotator(coord=['C', 'G'])
        theta, phi = r(theta, phi)

    index_global = hp.ang2pix(nside, theta, phi)

    if projection == 'healpix' and obspix is not None:
        npixsky = len(obspix)
        index_local = obspix.searchsorted(index_global)
        mask1 = index_local < npixsky
        loc = mask1
        loc[mask1] = obspix[index_local[mask1]] == index_global[mask1]
        outside_pixels = np.invert(loc)
        if (np.sum(outside_pixels) and (not cut_outliers)):
            raise ValueError(
                "Pixels outside patch boundaries. Patch width insufficient")
        else:
            index_local[outside_pixels] = -1
    elif projection == 'flat':
        x, y = input_sky.LamCyl(ra, dec)

        xminmap = xmin - pixel_size / 2.0
        yminmap = ymin - pixel_size / 2.0

        ix = np.int_((x - xminmap) / pixel_size)
        iy = np.int_((y - yminmap) / pixel_size)

        index_local = ix * npix_per_row + iy

        outside = (ix < 0) | (ix >= npix_per_row) | \
            (iy < 0) | (iy >= npix_per_row)
        index_local[outside] = - 1
    else:
        index_local = None

    return index_global, index_local

def load_fake_instrument(nside=16, nsquid_per_mux=1):
    """
    For test purposes.
    Create instances of HealpixFitsMap, hardware, and
    scanning_strategy to feed TimeOrderedDataPairDiff in tests.

    Returns
    ----------
    hardware : Hardware instance
        Instance of Hardware containing instrument parameters and models.
    scanning_strategy : ScanningStrategy instance
        Instance of ScanningStrategy containing scan parameters.
    HealpixFitsMap : HealpixFitsMap instance
        Instance of HealpixFitsMap containing input sky parameters.
    """
    ## Add paths to load modules
    sys.path.insert(0, os.path.realpath(os.path.join(os.getcwd(), '.')))
    sys.path.insert(0, os.path.realpath(os.path.join(os.getcwd(), 's4cmb')))
    from s4cmb.input_sky import HealpixFitsMap
    from s4cmb.instrument import Hardware
    from s4cmb.scanning_strategy import ScanningStrategy

    ## Create fake inputs

    ## Sky
    sky_in = HealpixFitsMap('s4cmb/data/test_data_set_lensedCls.dat',
                            do_pol=True, fwhm_in=0.0,
                            nside_in=nside, map_seed=48584937,
                            verbose=False, no_ileak=False, no_quleak=False)

    ## Instrument
    inst = Hardware(ncrate=1, ndfmux_per_crate=1,
                    nsquid_per_mux=nsquid_per_mux, npair_per_squid=4,
                    fp_size=60., fwhm=3.5,
                    beam_seed=58347, projected_fp_size=3., pm_name='5params',
                    type_hwp='CRHWP', freq_hwp=2., angle_hwp=0., verbose=False)

    ## Scanning strategy
    scan = ScanningStrategy(nces=2, start_date='2013/1/1 00:00:00',
                            telescope_longitude='-67:46.816',
                            telescope_latitude='-22:56.396',
                            telescope_elevation=5200.,
                            name_strategy='deep_patch',
                            sampling_freq=1., sky_speed=0.4,
                            language='fortran')
    scan.run()

    return inst, scan, sky_in


if __name__ == "__main__":
    import doctest
    doctest.testmod()
