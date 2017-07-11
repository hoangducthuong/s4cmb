#!/usr/bin/python
"""
Script to simulate time-ordered data generated by a CMB experiment
scanning the sky.

Author: Julien Peloton, j.peloton@sussex.ac.uk
"""
import healpy as hp
import numpy as np
from numpy import cos
from numpy import sin
from numpy import tan

sec2deg = 360.0/86400.0
d2r = np.pi / 180.0

class tod():
    """ Class to handle Time-Ordered Data (TOD) """
    def __init__(self, hardware, scanning_strategy, HealpixFitsMap):
        """
        C'est parti!

        Parameters
        ----------
        hardware : hardware instance
            Instance of hardware containing instrument parameters and models.
        scanning_strategy : scanning_strategy instance
            Instance of scanning_strategy containing scan parameters.
        HealpixFitsMap : HealpixFitsMap instance
            Instance of HealpixFitsMap containing input sky parameters.
        """
        self.hardware = hardware
        self.scanning_strategy = scanning_strategy
        self.HealpixFitsMap = HealpixFitsMap

    def ComputeBoresightPointing(self):
        """
        Compute the boresight pointing for all the focal plane bolometers.
        """
        pass

    def get_tod(self):
        """
        Scan the input sky maps to generate timestreams.
        """
        pass

    def map_tod(self):
        """
        Project time-ordered data into sky maps.
        """

class pointing():
    """ """
    def __init__(self, az_enc, el_enc, time, value_params,
                 allowed_params='ia ie ca an aw', lat=-22.958):
        """
        Apply pointing model with parameters `value_params` and
        names `allowed_params` to encoder az,el. Order of terms is
        `value_params` is same as order of terms in `allowed_params`.

        Full list of parameters (Thx Fred!):
            an:  azimuth axis tilt north of vertical
            aw:  azimuth axis tilt west of vertical
            an2:  potato chip
            aw2:  potato chip
            npae:  not parallel azimuth/elevation
            ca:  angle of beam to boresight in azimuth
            ia:  azimuth encoder zero
            ie:  elevation encoder zero + angle of beam
                to boresight in elevation
            tf:  cosine flexure
            tfs:  sine flexure
            ref:  refraction
            dt:  timing error in seconds (requires lat argument)
            elt:  time from start elevation correction
            ta1: linear order structural thermal warping in azimuth
            te1: linear order structural thermal warping in elevation
            sa,sa2: solar radiation structural warping in azimuth
            se,se2: solar radiation structural warping in elevation

        Parameters
        ----------
        az_enc : 1d array
            Encoder azimuth in radians.
        el_enc : 1d array
            Encoder elevation in radians.
        time : 1d array
            Encoder time (UTC)
        value_params : 1d array
            Value of the pointing model parameters (see instrument.py).
            In degrees (see below for full description)
        allowed_params : list of string
            Name of the pointing model parameters used in `value_params`.
        lat : float, optional
            Latitude of the telescope, in degree.

        Examples
        ----------
        See hardware.py for more information on the pointing model.
        >>> allowed_params = 'ia ie ca an aw'
        >>> value_params = [10.28473073, 8.73953334, -15.59771781,
        ...     -0.50977716, 0.10858016]
        >>> az_enc = np.array([np.sin(2 * np.pi * i / 100)
        ...     for i in range(100)])
        >>> el_enc = np.ones(100) * 0.5
        >>> time = np.array([56293 + t/84000 for t in range(100)])
        >>> pointing = pointing(az_enc, el_enc, time, value_params,
        ...     allowed_params, lat=-22.)
        >>> print(az_enc[2:4], pointing.az[2:4])
        (array([ 0.12533323,  0.18738131]), array([ 0.11717842,  0.17922137]))
        """
        self.az_enc = az_enc
        self.el_enc = el_enc
        self.time = time
        self.value_params = value_params
        self.allowed_params = allowed_params
        self.lat = lat * d2r

        self.az, self.el = self.apply_pointing_model()

    def apply_pointing_model(self):
        """
        Apply pointing corrections specified by the pointing model.

        Returns
        ----------
        az : 1d array
            The corrected azimuth in arcminutes.
        el : 1d array
            The corrected elevation in arcminutes.
        """
        assert len(self.value_params) == len(self.allowed_params.split()), \
            AssertionError("Vector containing parameters " +
                           "(value_params) has to have the same " +
                           "length than the vector containing names " +
                           "(allowed_params).")

        ## Here are many parameters defining a pointing model.
        ## Of course, we do not use all of them. They are zero by default,
        ## and only those specified by the user will be used.
        params = {p: 0.0 for p in ['an', 'aw', 'an2', 'aw2', 'an4',
                                   'aw4', 'npae', 'ca', 'ia', 'ie', 'tf',
                                   'tfs', 'ref', 'dt', 'elt', 'ta1',
                                   'te1', 'sa', 'se', 'sa2',
                                   'se2', 'sta', 'ste', 'sta2', 'ste2']}

        for param in params:
            if param in self.allowed_params.split():
                index = self.allowed_params.split().index(param)
                params[param] = self.value_params[index]

        params['dt'] *= sec2deg

        ## Azimuth
        azd = -params['an'] * sin(self.az_enc) * sin(self.el_enc)
        azd -= params['aw'] * cos(self.az_enc) * sin(self.el_enc)

        azd -= -params['an2'] * sin(2 * self.az_enc) * sin(self.el_enc)
        azd -= params['aw2'] * cos(2 * self.az_enc) * sin(self.el_enc)

        azd -= -params['an4'] * sin(4 * self.az_enc) * sin(self.el_enc)
        azd -= params['aw4'] * cos(4 * self.az_enc) * sin(self.el_enc)

        azd += params['npae'] * sin(self.el_enc)
        azd -= params['ca']
        azd += params['ia'] * cos(self.el_enc)

        azd += params['dt'] * (
            -sin(self.lat) + cos(self.az_enc) *
            cos(self.lat) * tan(self.el_enc))

        ## Elevation
        eld = params['an'] * cos(self.az_enc)
        eld -= params['aw'] * sin(self.az_enc)
        eld -= params['an2'] * cos(2 * self.az_enc)
        eld -= params['aw2'] * sin(2 * self.az_enc)
        eld -= params['an4'] * cos(4 * self.az_enc)
        eld -= params['aw4'] * sin(4 * self.az_enc)

        eld -= params['ie']
        eld += params['tf'] * cos(self.el_enc)
        eld += params['tfs'] * sin(self.el_enc)
        eld -= params['ref'] / tan(self.el_enc)

        eld += -params['dt'] * cos(self.lat) * sin(self.az_enc)

        eld += params['elt'] * (self.time - np.min(self.time))

        ## Convert back in radian and apply to the encoder values.
        azd *= np.pi / (180.0 * 60.)
        eld *= np.pi / (180.0 * 60.)

        azd /= np.cos(self.el_enc)

        az = self.az_enc - azd
        el = self.el_enc - eld

        return az, el


if __name__ == "__main__":
    import doctest
    doctest.testmod()
