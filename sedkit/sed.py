"""
A module to produce spectral energy distributions
and calculate fundamental and atmospheric parameters

Author: Joe Filippazzo, jfilippazzo@stsci.edu
"""
import os
import glob
import shutil
from pkg_resources import resource_filename

import astropy.table as at
import astropy.units as q
import astropy.io.ascii as ii
import astropy.constants as ac
import numpy as np
from astropy.modeling import fitting
from astropy.io import fits
from astropy.coordinates import Angle, SkyCoord
from astroquery.vizier import Vizier
from astroquery.simbad import Simbad
from bokeh.io import export_png
from bokeh.plotting import figure, show
from bokeh.models import HoverTool, Range1d, ColumnDataSource
from dustmaps.bayestar import BayestarWebQuery
from svo_filters import svo

from . import utilities as u
from . import spectrum as sp
from . import isochrone as iso
from . import relations as rel
from . import modelgrid as mg


Vizier.columns = ["**", "+_r"]
SptRadius = rel.SpectralTypeRadius()


class SED:
    """
    A class to construct spectral energy distributions and calculate
    fundamental paramaters of stars

    Attributes
    ----------
    Lbol: astropy.units.quantity.Quantity
        The bolometric luminosity [erg/s]
    Lbol_sun: astropy.units.quantity.Quantity
        The bolometric luminosity [L_sun]
    Mbol: float
        The absolute bolometric magnitude
    SpT: float
        The string spectral type
    Teff: astropy.units.quantity.Quantity
        The effective temperature calculated from the SED
    Teff_bb: astropy.units.quantity.Quantity
        The effective temperature calculated from the blackbody fit
    abs_SED: sequence
        The [W, F, E] of the calculate absolute SED
    abs_phot_SED: sequence
        The [W, F, E] of the calculate absolute photometric SED
    abs_spec_SED: sequence
        The [W, F, E] of the calculate absolute spectroscopic SED
    age_max: astropy.units.quantity.Quantity
        The upper limit on the age of the target
    age_min: astropy.units.quantity.Quantity
        The lower limit on the age of the target
    app_SED: sequence
        The [W, F, E] of the calculate apparent SED
    app_phot_SED: sequence
        The [W, F, E] of the calculate apparent photometric SED
    app_spec_SED: sequence
        The [W, F, E] of the calculate apparent spectroscopic SED
    bb_source: str
        The [W, F, E] fit to calculate Teff_bb
    blackbody: astropy.modeling.core.blackbody
        The best fit blackbody function
    distance: astropy.units.quantity.Quantity
        The target distance
    fbol: astropy.units.quantity.Quantity
        The apparent bolometric flux [erg/s/cm2]
    flux_units: astropy.units.quantity.Quantity
        The desired flux density units
    gravity: str
        The surface gravity suffix
    mbol: float
        The apparent bolometric magnitude
    name: str
        The name of the target
    parallaxes: astropy.table.QTable
        The table of parallaxes
    photometry: astropy.table.QTable
        The table of photometry
    piecewise: sequence
        The list of all piecewise combined spectra for normalization
    radius: astropy.units.quantity.Quantity
        The target radius
    sources: astropy.table.QTable
        The table of sources (with only one row of cource)
    spectra: astropy.table.QTable
        The table of spectra
    spectral_type: float
        The numeric spectral type, where 0-99 corresponds to spectral
        types O0-Y9
    spectral_types: astropy.table.QTable
        The table of spectral types
    suffix: str
        The spectral type suffix
    syn_photometry: astropy.table.QTable
        The table of calcuated synthetic photometry
    wave_units: astropy.units.quantity.Quantity
        The desired wavelength units
    """
    def __init__(self, name='My Target', verbose=True, **kwargs):
        """
        Initialize an SED object

        Parameters
        ----------
        name: str (optional)
            A name for the target
        verbose: bool
            Print some diagnostic stuff
        """
        # Attributes with setters
        self._name = None
        self._ra = None
        self._dec = None
        self._age = None
        self._distance = None
        self._parallax = None
        self._radius = None
        self._spectral_type = None
        self._membership = None
        self._sky_coords = None
        self._evo_model = None
        self.evo_model = 'hybrid_solar_age'

        # Static attributes
        self.verbose = verbose
        self.search_radius = 15*q.arcsec

        # Book keeping
        self.calculated = False
        self.isochrone_radius = False

        # Set the default wavelength and flux units
        self._wave_units = q.um
        self._flux_units = q.erg/q.s/q.cm**2/q.AA
        self.units = [self._wave_units, self._flux_units, self._flux_units]
        self.min_phot = 999*q.um
        self.max_phot = 0*q.um
        self.min_spec = 999*q.um
        self.max_spec = 0*q.um

        # Attributes of arbitrary length
        self.all_names = []
        self._spectra = []
        self.stitched_spectra = []
        self.app_spec_SED = None
        self.abs_spec_SED = None
        self.app_phot_SED = None
        self.abs_phot_SED = None
        self.best_fit = []

        # Photometry setup
        phot_cols = ('band', 'eff', 'app_magnitude', 'app_magnitude_unc',
                     'app_flux', 'app_flux_unc', 'abs_magnitude',
                     'abs_magnitude_unc', 'abs_flux', 'abs_flux_unc',
                     'bandpass')
        phot_typs = ('U16', np.float16, np.float16, np.float16, float, float,
                     np.float16, np.float16, float, float, 'O')
        self.reddening = 0

        # Make empty photometry table
        self._photometry = at.QTable(names=phot_cols, dtype=phot_typs)
        for col in ['app_flux', 'app_flux_unc', 'abs_flux', 'abs_flux_unc']:
            self._photometry[col].unit = self._flux_units
        self._photometry['eff'].unit = self._wave_units
        self._photometry.add_index('band')

        # Make empty synthetic photometry table
        self._synthetic_photometry = at.QTable(names=phot_cols, dtype=phot_typs)
        for col in ['app_flux', 'app_flux_unc', 'abs_flux', 'abs_flux_unc']:
            self._synthetic_photometry[col].unit = self._flux_units
        self._synthetic_photometry['eff'].unit = self._wave_units
        self._synthetic_photometry.add_index('band')

        # Try to set attributes from kwargs
        for k, v in kwargs.items():
            setattr(self, k, v)
        self.name = name

        # Make a plot
        self.fig = figure()

        # Empty result attributes
        self.fbol = None
        self.mbol = None
        self.Teff = None
        self.Teff_bb = None
        self.Teff_evo = None
        self.Lbol = None
        self.Mbol = None
        self.Lbol_sun = None
        self.SpT = None
        self.SpT_fit = None
        self.mass = None
        self.logg = None
        self.bb_source = None
        self.blackbody = None

        # Default parameters
        if self.age is None:
            self.age = 6*q.Gyr, 4*q.Gyr

    def add_photometry(self, band, mag, mag_unc=None, **kwargs):
        """Add a photometric measurement to the photometry table

        Parameters
        ----------
        band: name, svo_filters.svo.Filter
            The bandpass name or instance
        mag: float
            The magnitude
        mag_unc: float (optional)
            The magnitude uncertainty
        """
        # Make sure the magnitudes are floats
        if not isinstance(mag, float):
            raise TypeError("Magnitude must be a float.")

        # Check the uncertainty
        if not isinstance(mag, (float, None)):
            raise TypeError("Magnitude uncertainty must be a float, NaN, or None.")

        # Make NaN if 0
        if (isinstance(mag_unc, (float, int)) and mag_unc == 0)\
        or isinstance(mag_unc, np.ma.core.MaskedConstant):
            mag_unc = np.nan

        # Get the bandpass
        if isinstance(band, str):
            bp = svo.Filter(band)
        elif isinstance(band, svo.Filter):
            bp, band = band, band.name
        else:
            print('Not a recognized bandpass:', band)

        # Convert bandpass to desired units
        bp.wave_units = self.wave_units

        # Drop the current band if it exists
        if band in self.photometry['band']:
            self.drop_photometry(band)

        # Apply the dereddening by subtracting the (bandpass
        # extinction vector)*(source dust column density)
        mag -= bp.ext_vector*self.reddening

        # Make a dict for the new point
        new_photometry = {'band': band, 'eff': bp.wave_eff,
                          'app_magnitude': mag, 'app_magnitude_unc': mag_unc,
                          'bandpass': bp}

        # Add the kwargs
        new_photometry.update(kwargs)

        # Add it to the table
        self._photometry.add_row(new_photometry)

        # Set SED as uncalculated
        self.calculated = False

        # Update photometry max and min wavelengths
        if self.min_phot is None or bp.wave_eff < self.min_phot:
            self.min_phot = bp.wave_eff
        if self.max_phot is None or bp.wave_eff > self.max_phot:
            self.max_phot = bp.wave_eff

    def add_photometry_file(self, file):
        """Add a table of photometry from an ASCII file that
        contains the columns 'band', 'magnitude', and 'uncertainty'

        Parameters
        ----------
        file: str
            The path to the ascii file
        """
        # Read the data
        table = ii.read(file)

        # Test to see if columns are present
        cols = ['band', 'magnitude', 'uncertainty']
        if not all([i in table.colnames for i in cols]):
            raise TableError('File must contain columns', cols)

        # Keep relevant cols
        table = table[cols]

        # Add the data to the SED object
        for row in table:

            # Add the magnitude
            self.add_photometry(*row)

    # def add_SDSS_spectrum(self, file=None, plate=None, mjd=None, fiber=None):
    #     """Add an SDSS spectrum to the SED from file or by
    #     the plate, mjd, and fiber numbers
    #
    #     Parameters
    #     ----------
    #     file: str
    #         The path to the file
    #     plate: int
    #         The plate of the SDSS spectrum
    #     mjd: int
    #         The MJD of the SDSS spectrum
    #     fiber: int
    #         The fiber of the SDSS spectrum
    #     """
    #     # Get the local file
    #     if file is not None:
    #         spec = SDSSfits(file)
    #
    #     elif plate is not None and mjd is not None and fiber is not None:
    #         spec = fetch_sdss_spectrum(plate, mjd, fiber)
    #
    #     else:
    #         raise ValueError('Huh?')
    #
    #     wave = spec.wavelength()*q.AA
    #     flux = spec.spectrum*1E-17*q.erg/q.s/q.cm**2/q.AA
    #     unc = spec.error*1E-17*q.erg/q.s/q.cm**2/q.AA
    #
    #     # Add the data to the SED object
    #     self.add_spectrum([wave, flux, unc])

    def add_spectrum(self, spectrum, **kwargs):
        """Add a new Spectrum object to the SED

        Parameters
        ----------
        spectrum: sequence, sedkit.spectrum.Spectrum
            A sequence of [W,F] or [W,F,E] with astropy units
            or a Spectrum object
        """
        if isinstance(spectrum, sp.Spectrum):
            spec = spectrum

        elif isinstance(spectrum, (list, tuple)):

            # Create the Spectrum object
            if len(spectrum) in [2, 3]:
                spec = sp.Spectrum(*spectrum, **kwargs)

            else:
                raise ValueError('Input spectrum must be [W,F] or [W,F,E].')

        else:
            raise TypeError('Must enter [W,F], [W,F,E], or a Spectrum object')

        # Convert to SED units
        spec.wave_units = self.wave_units
        spec.flux_units = self.flux_units

        # Add the spectrum object to the list of spectra
        self._spectra.append(spec)

        # Set SED as uncalculated
        self.calculated = False

        # Update spectra max and min wavelengths
        if self.min_spec is None or np.nanmin(spec.spectrum[0]) < self.min_spec:
            self.min_spec = np.nanmin(spec.spectrum[0])
        if self.max_spec is None or np.nanmax(spec.spectrum[0]) > self.max_spec:
            self.max_spec = np.nanmax(spec.spectrum[0])

    def add_spectrum_file(self, file, wave_units=None, flux_units=None, ext=0,
                          survey=None, **kwargs):
        """Add a spectrum from an ASCII or FITS file

        Parameters
        ----------
        file: str
            The path to the ascii file
        wave_units: astropy.units.quantity.Quantity
            The wavelength units
        flux_units: astropy.units.quantity.Quantity
            The flux units
        ext: int, str
            The FITS extension name or index
        survey: str (optional)
            The name of the survey
        """
        # Generate a FileSpectrum
        spectrum = sp.FileSpectrum(file, wave_units=wave_units,
                                   flux_units=flux_units,
                                   ext=ext, survey=survey, **kwargs)

        # Add the data to the SED object
        self.add_spectrum(spectrum, **kwargs)

    @property
    def age(self):
        """A property for age"""
        return self._age

    @age.setter
    def age(self, age):
        """A setter for age"""
        # Make sure it's a sequence
        if not isinstance(age, (tuple, list, np.ndarray))\
        or len(age) not in [2, 3]:
            raise TypeError('Age must be a sequence of (value, error) or\
                            (value, lower_error, upper_error).')

        # Make sure the values are in time units
        if not age[0].unit.is_equivalent(q.Gyr):
            raise TypeError("Age values must be time units of\
                             astropy.units.quantity.Quantity, e.g. 'Gyr'")

        # Set the age!
        self._age = age

        if self.verbose:
            print('Setting age to', self.age)

        # Set SED as uncalculated
        self.calculated = False

    def _calculate_sed(self):
        """Stitch the components together and flux calibrate if possible
        """
        # Construct full app_SED
        self.app_SED = np.sum([self.wein, self.app_specphot_SED,
                               self.rj]+self.stitched_spectra)

        # Flux calibrate SEDs
        if self.distance is not None:
            self.abs_SED = self.app_SED.flux_calibrate(self.distance)

        # Calculate Fundamental Params
        self.fundamental_params()

    def calculate_synthetic_mags(self):
        """Calculate synthetic magnitudes of all stitched spectra"""
        if len(self.stitched_spectra)>0:

            # Iterate over spectra
            for spec in self.stitched_spectra:

                # and over bandpasses
                for band in s.BANDPASSES:

                    # Get the bandpass
                    bp = svo.Filter(band)

                    # Check for overlap before calculating
                    if bp.overlap(spec) in ['full', 'partial']:

                        # Calculate the magnitiude
                        mag, mag_unc = spec.synthetic_mag(bp)

                        # Make a dict for the new point
                        new_photometry = {'band':band, 'eff':bp.eff,
                                          'bandpass':bp, 'app_magnitude':mag,
                                          'app_magnitude_unc':mag_unc}

                        # Add it to the table
                        self._synthetic_photometry.add_row(new_photometry)

    def _calibrate_photometry(self):
        """Calculate the absolute magnitudes and flux values of all rows in 
        the photometry table
        """
        # Reset absolute photometry
        self._photometry['abs_flux'] = np.nan
        self._photometry['abs_flux_unc'] = np.nan
        self._photometry['abs_magnitude'] = np.nan
        self._photometry['abs_magnitude_unc'] = np.nan
        self.abs_phot_SED = None

        if self.photometry is not None and len(self.photometry)>0:

            # Update the photometry
            self._photometry['eff'] = self._photometry['eff'].to(self.wave_units)
            self._photometry['app_flux'] = self._photometry['app_flux'].to(self.flux_units)
            self._photometry['app_flux_unc'] = self._photometry['app_flux_unc'].to(self.flux_units)
            self._photometry['abs_flux'] = self._photometry['abs_flux'].to(self.flux_units)
            self._photometry['abs_flux_unc'] = self._photometry['abs_flux_unc'].to(self.flux_units)

            # Get the app_mags
            m = np.array(self._photometry)['app_magnitude']
            m_unc = np.array(self._photometry)['app_magnitude_unc']

            # Calculate app_flux values
            for n, row in enumerate(self._photometry):
                app_flux, app_flux_unc = u.mag2flux(row['bandpass'], row['app_magnitude'], sig_m=row['app_magnitude_unc'])
                self._photometry['app_flux'][n] = app_flux.to(self.flux_units)
                self._photometry['app_flux_unc'][n] = app_flux_unc.to(self.flux_units)

            # Calculate absolute mags
            if self.distance is not None:

                # Calculate abs_mags
                M, M_unc = u.flux_calibrate(m, self.distance[0], m_unc, self.distance[1])
                self._photometry['abs_magnitude'] = M
                self._photometry['abs_magnitude_unc'] = M_unc

                # Calculate abs_flux values
                for n, row in enumerate(self._photometry):
                    abs_flux, abs_flux_unc = u.mag2flux(row['bandpass'], row['abs_magnitude'], sig_m=row['abs_magnitude_unc'])
                    self._photometry['abs_flux'][n] = abs_flux.to(self.flux_units)
                    self._photometry['abs_flux_unc'][n] = abs_flux_unc.to(self.flux_units)

            # Make apparent photometric SED with photometry
            app_cols = ['eff', 'app_flux', 'app_flux_unc']
            phot_array = np.array(self.photometry[app_cols])
            phot_array = phot_array[(self.photometry['app_flux']>0)&(self.photometry['app_flux_unc']>0)]
            self.app_phot_SED = sp.Spectrum(*[phot_array[i]*Q for i, Q in zip(app_cols, self.units)])

            # Make absolute photometric SED with photometry
            if self.distance is not None:
                self.abs_phot_SED = self.app_phot_SED.flux_calibrate(self.distance)

        # Set SED as uncalculated
        self.calculated = False

    def _calibrate_spectra(self):
        """Create composite spectra and flux calibrate
        """
        # Reset absolute spectra
        self.abs_spec_SED = None

        if self.spectra is not None and len(self.spectra) > 0:

            # Update the spectra
            for spectrum in self.spectra:
                spectrum.flux_units = self.flux_units

            # Group overlapping spectra and stitch together where possible
            # to form peacewise spectrum for flux calibration
            self.stitched_spectra = []
            if len(self.spectra) > 1:
                groups = self.group_spectra(self.spectra)
                self.stitched_spectra = [np.sum(group) if len(group) > 1\
                                         else group[0] for group in groups]

            # If one spectrum, no need to make composite
            elif len(self.spectra) == 1:
                self.stitched_spectra = self.spectra

            # If no spectra, forget it
            else:
                self.stitched_spectra = []
                print('No spectra available for SED.')

            # Renormalize the stitched spectra
            if len(self.photometry) > 0:
                self.stitched_spectra = [i.norm_to_mags(self.photometry)\
                                         for i in self.stitched_spectra]

            # Make apparent spectral SED
            if len(self.stitched_spectra) > 1:
                self.app_spec_SED = np.sum(self.stitched_spectra)
            elif len(self.stitched_spectra) == 1:
                self.app_spec_SED = self.stitched_spectra[0]
            else:
                self.app_spec_SED = None

            # Make absolute spectral SED
            if self.app_spec_SED is not None and self.distance is not None:
                self.abs_spec_SED = self.app_spec_SED.flux_calibrate(self.distance)

        # Set SED as uncalculated
        self.calculated = False

    @property
    def dec(self):
        """A property for declination"""
        return self._dec

    @dec.setter
    def dec(self, dec, dec_unc=None, frame='icrs'):
        """Set the declination of the source

        Padecmeters
        ----------
        dec: astropy.units.quantity.Quantity
            The declination
        dec_unc: astropy.units.quantity.Quantity (optional)
            The uncertainty
        frame: str
            The reference frame
        """
        if not isinstance(dec, (q.quantity.Quantity, str)):
            raise TypeError("Cannot interpret dec :", dec)

        # Make sure it's decimal degrees
        self._dec = Angle(dec)
        if self.ra is not None:
            self.sky_coords = self.ra, self.dec

    @property
    def distance(self):
        """A property for distance"""
        return self._distance

    @distance.setter
    def distance(self, distance):
        """A setter for distance

        Parameters
        ----------
        distance: sequence
            The (distance, err) or (distance, lower_err, upper_err)
        """
        if distance is None:

            self._distance = None
            self._parallax = None

            # Only clear radius if determined from isochrones,
            # otherwise keep it if manually set
            if self.isochrone_radius:
                self.radius = None
                self.isochrone_radius = False

        else:
            # Make sure it's a sequence
            typs = (tuple, list, np.ndarray)
            if not isinstance(distance, typs) or len(distance) not in [2, 3]:
                raise TypeError('Distance must be a sequence of (value, error) or (value, lower_error, upper_error).')

            # Make sure the values are in time units
            if not distance[0].unit.is_equivalent(q.pc):
                raise TypeError("Distance values must be length units of astropy.units.quantity.Quantity, e.g. 'pc'")

            # Set the distance
            self._distance = distance

            if self.verbose:
                print('Setting distance to', self.distance)

            # Update the parallax
            self._parallax = u.pi2pc(*self.distance, pc2pi=True)

        # Try to calculate reddening
        self.get_reddening()

        # Update the absolute photometry
        self._calibrate_photometry()

        # Update the flux calibrated spectra
        self._calibrate_spectra()

        # Set SED as uncalculated
        self.calculated = False

    def drop_photometry(self, band):
        """Drop a photometry by its index or name in the photometry list

        Parameters
        ----------
        band: str, int
            The bandpass name or index to drop
        """
        if isinstance(band, str) and band in self.photometry['band']:
            band = self._photometry.remove_row(np.where(self._photometry['band'] == band)[0][0])

        if isinstance(band, int) and band<=len(self._photometry):
            self._photometry.remove_row(band)

        # Set SED as uncalculated
        self.calculated = False

    def drop_spectrum(self, idx):
        """Drop a spectrum by its index in the spectra list

        Parameters
        ----------
        idx: int
            The index of the spectrum to drop
        """
        self._spectra = [i for n, i in enumerate(self._spectra) if n!=idx]

        # Set SED as uncalculated
        self.calculated = False

    @property
    def evo_model(self):
        """A getter for the evolutionary model"""
        return self._evo_model

    @evo_model.setter
    def evo_model(self, model):
        """A setter for the evolutionary model

        Parameters
        ----------
        model: str
            The evolutionary model name
        """
        if model not in iso.EVO_MODELS:
            raise IOError("Please use an evolutionary model from the list: {}".format(mg.EVO_MODELS))

        self._evo_model = iso.Isochrone(model)

    def export(self, parentdir='.', dirname=None, zipped=False):
        """
        Exports the photometry and results tables and a file of the
        composite spectra

        Parameters
        ----------
        parentdir: str
            The parent directory for the folder or zip file
        dirname: str (optional)
            The name of the exported directory or zip file, default is SED name
        zipped: bool
            Zip the directory
        """
        # Check the parent directory
        if not os.path.exists(parentdir):
            raise IOError('No such target directory', parentdir)

        # Check the target directory
        name = self.name.replace(' ', '_')
        dirname = dirname or name
        dirpath = os.path.join(parentdir, dirname)
        if not os.path.exists(dirpath):
            os.system('mkdir {}'.format(dirpath))
        else:
            raise IOError('Directory already exists:', dirpath)

        # Apparent spectral SED
        if self.app_spec_SED is not None:
            specpath = os.path.join(dirpath, '{}_apparent_SED.txt'.format(name))
            header = '{} apparent spectrum (erg/s/cm2/A) as a function of wavelength (um)'.format(name)
            spec_data = self.app_spec_SED.spectrum
            np.savetxt(specpath, np.asarray(spec_data).T, header=header)

        # Absolute spectral SED
        if self.abs_spec_SED is not None:
            specpath = os.path.join(dirpath, '{}_absolute_SED.txt'.format(name))
            header = '{} absolute spectrum (erg/s/cm2/A) as a function of wavelength (um)'.format(name)
            spec_data = self.abs_spec_SED.spectrum
            np.savetxt(specpath, np.asarray(spec_data).T, header=header)

        # All photometry
        if self.photometry is not None:
            photpath = os.path.join(dirpath,'{}_photometry.txt'.format(name))
            self.photometry.write(photpath, format='ipac')

        # All results
        resultspath = os.path.join(dirpath,'{}_results.txt'.format(name))
        self.results.write(resultspath, format='ipac')

        # The SED plot
        if self.fig is not None:
            pltopath = os.path.join(dirpath,'{}_plot.png'.format(name))
            export_png(self.fig, filename=pltopath)

        # zip if desired
        if zipped:
            shutil.make_archive(dirpath, 'zip', dirpath)
            os.system('rm -R {}'.format(dirpath))

    def find_2MASS(self, **kwargs):
        """
        Search for 2MASS data
        """
        self.find_photometry('2MASS', 'II/246/out',
                             ['Jmag', 'Hmag', 'Kmag'],
                             ['2MASS.J', '2MASS.H', '2MASS.Ks'],
                             **kwargs)

    def find_Gaia(self, search_radius=15*q.arcsec, catalog='I/345/gaia2'):
        """
        Search for Gaia data

        Parameters
        ----------
        search_radius: astropy.units.quantity.Quantity
            The radius for the cone search
        catalog: str
            The Vizier catalog to search
        """
        # Make sure there are coordinates
        if not isinstance(self.sky_coords, SkyCoord):
            raise TypeError("Can't find Gaia data without coordinates!")

        # Query the catalog
        parallaxes = Vizier.query_region(self.sky_coords, radius=search_radius or self.search_radius, catalog=[catalog])

        # Parse the records
        if parallaxes:

            # Grab the first record
            parallax = list(parallaxes[0][0][['Plx', 'e_Plx']])
            self.parallax = parallax[0]*q.mas, parallax[1]*q.mas

            # Get Gband while we're here
            try:
                mag, unc = list(parallaxes[0][0][['Gmag', 'e_Gmag']])
                self.add_photometry('Gaia.G', mag, unc)
            except:
                pass

    def find_PanSTARRS(self, **kwargs):
        """
        Search for PanSTARRS data
        """
        self.find_photometry('PanSTARRS', 'II/349/ps1',
                             ['gmag', 'rmag', 'imag', 'zmag', 'ymag'],
                             ['PS1.g', 'PS1.r', 'PS1.i', 'PS1.z', 'PS1.y'],
                             **kwargs)

    def find_photometry(self, name, catalog, band_names, target_names=None, search_radius=None, idx=0, **kwargs):
        """
        Search Vizier for photometry in the given catalog

        Parameters
        ----------
        name: str
            The informal name of the catalog, e.g. '2MASS'
        catalog: str
            The Vizier catalog address, e.g. 'II/246/out'
        band_names: sequence
            The list of column names to treat as bandpasses
        target_names: sequence (optional)
            The list of renamed columns, must be the same length as band_names
        search_radius: astropy.units.quantity.Quantity
            The search radius for the Vizier query
        idx: int
            The index of the record to use if multiple Vizier results
        """
        # Make sure there are coordinates
        if not isinstance(self.sky_coords, SkyCoord):
            raise TypeError("Can't find {} photometry without coordinates!".format(name))

        # See if the designation was fetched by Simbad
        des = [name for name in self.all_names if name.startswith(name)]

        # Get photometry using designation...
        if len(des) > 0:
            viz_cat = Vizier.query_object(des[0], catalog=[catalog])

        # ...or from the coordinates
        else:
            rad = search_radius or self.search_radius
            viz_cat = Vizier.query_region(self.sky_coords, radius=rad, catalog=[catalog])

        if target_names is None:
            target_names = band_names

        # Parse the record
        if len(viz_cat) > 0:
            if len(viz_cat) > 1:
                print('{} {} records found.'.format(len(viz_cat), name))

            # Grab the record
            rec = viz_cat[0][idx]

            # Pull out the photometry
            for band, viz in zip(target_names, band_names):
                try:
                    mag, unc = list(rec[[viz, 'e_'+viz]])
                    mag, unc = round(float(mag), 3), round(float(unc), 3)
                    self.add_photometry(band, mag, unc)
                except IOError:
                    pass

    def find_SDSS(self, **kwargs):
        """
        Search for SDSS data
        """
        self.find_photometry('SDSS', 'V/147',
                             ['umag', 'gmag', 'rmag', 'imag', 'zmag'],
                             ['SDSS.u', 'SDSS.g', 'SDSS.r', 'SDSS.i', 'SDSS.z'],
                             **kwargs)

    def find_SIMBAD(self, search_radius=10*q.arcsec):
        """
        Search for a SIMBAD record

        Parameters
        ----------
        search_radius: astropy.units.quantity.Quantity
            The radius for the cone search
        """
        # Check for coordinates
        if isinstance(self.sky_coords, SkyCoord):

            # Search Simbad by sky coords
            rad = search_radius or self.search_radius
            viz_cat = Simbad.query_region(self.sky_coords, radius=rad)

        elif self.name is not None and self.name != 'My Target':

            viz_cat = Simbad.query_object(self.name)

        else:
            return

        # Parse the record and save the names
        if viz_cat is not None:
            main_ID = viz_cat[0]['MAIN_ID'].decode("utf-8")
            self.all_names += list(Simbad.query_objectids(main_ID)['ID'])

            # Remove duplicates
            self.all_names = list(set(self.all_names))

            if self.name is None:
                self.name = main_ID

            if self.sky_coords is None:
                self.sky_coords = tuple(viz_cat[0][['RA', 'DEC']])

    def find_WISE(self, **kwargs):
        """
        Search for WISE data
        """
        self.find_photometry('WISE', 'II/328/allwise',
                             ['W1mag', 'W2mag', 'W3mag', 'W4mag'],
                             ['WISE.W1', 'WISE.W2', 'WISE.W3', 'WISE.W4'],
                             **kwargs)

    def fit_blackbody(self, fit_to='app_phot_SED', Teff_init=4000, epsilon=0.0001, acc=0.05, trim=[], norm_to=[]):
        """
        Fit a blackbody curve to the data

        Parameters
        ----------
        fit_to: str
            The attribute name of the [W, F, E] to fit
        initial: int
            The initial guess
        epsilon: float
            The step size
        acc: float
            The acceptible error
        """
        if not self.calculated:
            self.make_sed()

        # Get the data and remove NaNs
        data = u.scrub(getattr(self, fit_to).data)

        # Trim manually
        if isinstance(trim, (list, tuple)):
            for mn, mx in trim:
                try:
                    idx, = np.where((data[0] < mn) | (data[0] > mx))
                    if any(idx):
                        data = [i[idx] for i in data]
                except TypeError:
                    print('Please provide a list of (lower, upper) bounds to exclude from the fit, e.g. [(0, 0.8)]')

        # Initial guess
        if self.Teff is not None:
            teff = self.Teff[0].value
        else:
            teff = Teff_init
        init = u.blackbody(temperature=teff)

        # Fit the blackbody
        fit = fitting.LevMarLSQFitter()
        norm = np.nanmax(data[1])
        weight = norm/data[2]
        if acc is None:
            acc = np.nanmax(weight)
        bb_fit = fit(init, data[0], data[1]/norm, weights=weight,
                     epsilon=epsilon, acc=acc, maxiter=500)

        # Store the results
        try:
            self.Teff_bb = int(bb_fit.temperature.value)
            self.bb_source = fit_to
            self.bb_norm_to = norm_to

            # Make the blackbody spectrum
            wav = np.linspace(0.2, 22., 400)*self.wave_units
            bb = sp.Blackbody(wav, self.Teff_bb*q.K, radius=self.radius,
                              distance=self.distance)
            bb = bb.norm_to_mags(self.photometry[-3:], include=norm_to)
            self.blackbody = bb

            if self.verbose:
                print('\nBlackbody fit: {} K'.format(self.Teff_bb))
        except IOError:
            if self.verbose:
                print('\nNo blackbody fit.')

    def fit_modelgrid(self, modelgrid):
        """Fit a model grid to the composite spectra

        Parameters
        ----------
        modelgrid: sedkit.modelgrid.ModelGrid
            The model grid to fit
        """
        if not self.calculated:
            self.make_sed()

        if self.app_spec_SED is not None:

            self.app_spec_SED.best_fit_model(modelgrid)
            self.best_fit = self.app_spec_SED.best_fit

            if self.verbose:
                print('Best fit: ',
                      self.best_fit[-1][modelgrid.parameters][0])

        else:
            print("Sorry, could not fit SED to model grid", modelgrid)

    def fit_spectral_type(self):
        """Fit the spectral SED to a catalog of spectral standards"""
        # Grab the SPL
        spl = mg.SpexPrismLibrary()

        # Run the fit
        self.fit_modelgrid(spl)

        # Store the result
        self.SpT_fit = self.app_spec_SED.best_fit[-1].spty

    @property
    def flux_units(self):
        """A property for flux_units"""
        return self._flux_units

    @flux_units.setter
    def flux_units(self, flux_units):
        """A setter for flux_units

        Parameters
        ----------
        flux_units: astropy.units.quantity.Quantity
            The astropy units of the SED wavelength
        """
        # Make sure it's a quantity
        if not isinstance(flux_units, (q.core.PrefixUnit, q.core.Unit, q.core.CompositeUnit)):
            raise TypeError('flux_units must be astropy.units.quantity.Quantity')

        # Make sure the values are in length units
        try:
            flux_units.to(q.erg/q.s/q.cm**2/q.AA)
        except:
            raise TypeError("flux_units must be a unit of flux density, e.g. 'erg/s/cm2/A'")

        # fnu2flam(f_nu, lam, units=q.erg/q.s/q.cm**2/q.AA)

        # Set the flux_units!
        self._flux_units = flux_units
        self.units = [self._wave_units, self._flux_units, self._flux_units]

        # Recalibrate the data
        self._calibrate_photometry()
        self._calibrate_spectra()

    def from_database(self, db, rename_bands=u.PHOT_ALIASES, **kwargs):
        """
        Load the data from an astrodbkit.astrodb.Database

        Parameters
        ----------
        db: astrodbkit.astrodb.Database
            The database instance to query
        rename_bands: dict
            A lookup dictionary to map database bandpass
            names to sedkit required bandpass names,
            e.g. {'2MASS_J': '2MASS.J', 'WISE_W1': 'WISE.W1'}

        Example
        -------
        from sedkit import SED
        from astrodbkit.astrodb import Database
        db = Database('/Users/jfilippazzo/Documents/Modules/BDNYCdb/bdnyc_database.db')
        s = SED()
        s.from_database(db, source_id=710, photometry='*', spectra=[1639], parallax=49)
        s.spectral_type = 'M9'
        s.fit_spectral_type()
        print(s.results)
        s.plot(draw=True)
        """
        # Check that astrodbkit is imported
        if not hasattr(db, 'query'):
            raise TypeError("Please provide an astrodbkit.astrodb.Database\
                             object to query.")

        # Get the metadata
        if 'source_id' in kwargs:

            if not isinstance(kwargs['source_id'], int):
                raise TypeError("'source_id' must be an integer")

            self.source_id = kwargs['source_id']
            source = db.query("SELECT * FROM sources WHERE id=?",
                              (self.source_id, ), fmt='dict', fetch='one')

            # Set the name
            self.name = source.get('designation', source.get('names', self.name))

            # Set the coordinates
            ra = source.get('ra')*q.deg
            dec = source.get('dec')*q.deg
            self.sky_coords = SkyCoord(ra=ra, dec=dec, frame='icrs')

        # Get the photometry
        if 'photometry' in kwargs:

            if kwargs['photometry'] == '*':
                phot_q = "SELECT * FROM photometry WHERE source_id={}".format(self.source_id)
                phot = db.query(phot_q, fmt='dict')

            elif isinstance(kwargs['photometry'], (list, tuple)):
                phot_ids = tuple(kwargs['photometry'])
                phot_q = "SELECT * FROM photometry WHERE id IN ({})".format(', '.join(['?']*len(phot_ids)))
                phot = db.query(phot_q, phot_ids, fmt='dict')

            else:
                raise TypeError("'photometry' must be a list of integers or '*'")

            # Add the bands
            for row in phot:

                # Make sure the bandpass name is right
                if row['band'] in rename_bands:
                    row['band'] = rename_bands.get(row['band'])

                self.add_photometry(row['band'], row['magnitude'],
                                    row['magnitude_unc'])

        # Get the parallax
        if 'parallax' in kwargs:

            if not isinstance(kwargs['parallax'], int):
                raise TypeError("'parallax' must be an integer")

            plx = db.query("SELECT * FROM parallaxes WHERE id=?",
                           (kwargs['parallax'], ), fmt='dict', fetch='one')

            # Add it to the object
            self.parallax = plx['parallax']*q.mas, plx['parallax_unc']*q.mas

        # Get the spectral type
        if 'spectral_type' in kwargs:

            if not isinstance(kwargs['spectral_type'], int):
                raise TypeError("'spectral_type' must be an integer")

            spt_id = kwargs['spectral_type']
            spt = db.query("SELECT * FROM spectral_types WHERE id=?",
                           (spt_id, ), fmt='dict', fetch='one')

            # Add it to the object
            spectral_type = spt.get('spectral_type')
            spectral_type_unc = spt.get('spectral_type_unc', 0.5)
            gravity = spt.get('gravity')
            lum_class = spt.get('lum_class', 'V')
            prefix = spt.get('prefix')

            # Add it to the object
            self.spectral_type = spectral_type, spectral_type_unc, gravity, lum_class, prefix

        # Get the spectra
        if 'spectra' in kwargs:

            if kwargs['spectra'] == '*':
                spec_q = "SELECT * FROM spectra WHERE source_id={}".format(self.source_id)
                spec = db.query(spec_q, fmt='dict')

            elif isinstance(kwargs['spectra'], (list, tuple)):
                spec_ids = tuple(kwargs['spectra'])
                spec_q = "SELECT * FROM spectra WHERE id IN ({})".format(', '.join(['?']*len(spec_ids)))
                spec = db.query(spec_q, spec_ids, fmt='dict')

            else:
                raise TypeError("'spectra' must be a list of integers or '*'")

            # Add the spectra
            for row in spec:

                # Make the Spectrum object
                wav, flx, unc = row['spectrum'].data
                wave_unit = u.str2Q(row['wavelength_units'])
                if row['flux_units'].startswith('norm'):
                    flux_unit = self.flux_units
                else:
                    flux_unit = u.str2Q(row['flux_units'])

                # Add the spectrum to the object
                self.add_spectrum([wav*wave_unit, flx*flux_unit, unc*flux_unit])

    def fundamental_params(self, **kwargs):
        """
        Calculate the fundamental parameters of the current SED
        """
        # Calculate bolometric luminosity (dependent on fbol and distance)
        self.get_Lbol()
        self.get_Mbol()

        # Interpolate surface gravity, mass and radius from isochrones
        if self.Lbol_sun is not None:

            if self.Lbol_sun[1] is None:
                print('Lbol={0.Lbol}. Uncertainties are needed to estimate Teff, radius, surface gravity, and mass.'.format(self))

            else:
                if self.radius is None:
                    self.radius_from_age()
                self.logg_from_age()
                self.mass_from_age()
                self.teff_from_age()

        # Calculate Teff (dependent on Lbol, distance, and radius)
        self.get_Teff()

    def get_fbol(self, units=q.erg/q.s/q.cm**2):
        """Calculate the bolometric flux of the SED

        Parameters
        ----------
        units: astropy.units.quantity.Quantity
            The target untis for fbol
        """
        # Integrate the SED to get fbol
        self.fbol = self.app_SED.integral(units=units)

    def get_Lbol(self):
        """Calculate the bolometric luminosity of the SED
        """
        # Caluclate fbol if not present
        if self.fbol is None:
            self.get_fbol()

        # Calculate Lbol
        if self.distance is not None:
            Lbol = (4*np.pi*self.fbol[0]*self.distance[0]**2).to(q.erg/q.s)
            Lbol_sun = round(np.log10((Lbol/ac.L_sun).decompose().value), 3)

            # Calculate Lbol_unc
            if self.fbol[1] is None:
                Lbol_unc = None
                Lbol_sun_unc = None
            else:
                Lbol_unc = Lbol*np.sqrt((self.fbol[1]/self.fbol[0]).value**2+(2*self.distance[1]/self.distance[0]).value**2)
                Lbol_sun_unc = round(abs(Lbol_unc/(Lbol*np.log(10))).value, 3)

            # Update the attributes
            self.Lbol = Lbol, Lbol_unc
            self.Lbol_sun = Lbol_sun, Lbol_sun_unc

    def get_mbol(self, L_sun=3.86E26*q.W, Mbol_sun=4.74):
        """Calculate the apparent bolometric magnitude of the SED

        Parameters
        ----------
        L_sun: astropy.units.quantity.Quantity
            The bolometric luminosity of the Sun
        Mbol_sun: float
            The absolute bolometric magnitude of the sun
        """
        # Calculate fbol if not present
        if self.fbol is None:
            self.get_fbol()

        # Calculate mbol
        mbol = round(-2.5*np.log10(self.fbol[0].value)-11.482, 3)

        # Calculate mbol_unc
        if self.fbol[1] is None:
            mbol_unc = None
        else:
            mbol_unc = round((2.5/np.log(10))*(self.fbol[1]/self.fbol[0]).value, 3)

        # Update the attribute
        self.mbol = mbol, mbol_unc

    def get_Mbol(self):
        """Calculate the absolute bolometric magnitude of the SED
        """
        # Calculate mbol if not present
        if self.mbol is None:
            self.get_mbol()

        # Calculate Mbol
        if self.distance is not None:
            Mbol = round(self.mbol[0]-5*np.log10((self.distance[0]/10*q.pc).value), 3)

            # Calculate Mbol_unc
            if self.fbol[1] is None:
                Mbol_unc = None
            else:
                Mbol_unc = round(np.sqrt(self.mbol[1]**2+((2.5/np.log(10))*(self.distance[1]/self.distance[0]).value)**2), 3)

            # Update the attribute
            self.Mbol = Mbol, Mbol_unc

    def get_reddening(self):
        """Calculate the reddening from the Bayestar17 dust map"""
        if self.distance is not None and self.sky_coords is not None:
            gal_coords = SkyCoord(self.sky_coords.galactic, frame='galactic', distance=self.distance[0])
            bayestar = BayestarWebQuery(version='bayestar2017')
            self.reddening = bayestar(gal_coords, mode='random_sample')

    def get_Teff(self):
        """Calculate the effective temperature
        """
        # Calculate Teff
        if self.distance is not None and self.radius is not None:
            Teff = np.sqrt(np.sqrt((self.Lbol[0]/(4*np.pi*ac.sigma_sb*self.radius[0]**2)).to(q.K**4))).astype(int)

            # Calculate Teff_unc
            if self.fbol[1] is None:
                Teff_unc = None
            else:
                Teff_unc = (Teff*np.sqrt((self.Lbol[1]/self.Lbol[0]).value**2 + (2*self.radius[1]/self.radius[0]).value**2)/4.).astype(int)

            # Update the attribute
            self.Teff = Teff, Teff_unc

    @staticmethod
    def group_spectra(spectra):
        """Puts a list of *spectra* into groups with overlapping wavelength arrays
        """
        groups, idx = [], []
        for N, S in enumerate(spectra):
            if N not in idx:
                group, idx = [S], idx + [N]
                for n, s in enumerate(spectra):
                    if n not in idx and any(np.where(np.logical_and(S.wave<s.wave[-1], S.wave>s.wave[0]))[0]):
                        group.append(s), idx.append(n)
                groups.append(group)
        return groups

    @property
    def info(self):
        """
        Print all the SED info
        """
        for attr in dir(self):
            if not attr.startswith('_') and attr not in ['info', 'results'] and not callable(getattr(self, attr)):
                val = getattr(self, attr)
                print('{0: <25}= {1}{2}'.format(attr, '\n' if isinstance(val, at.QTable) else '', val))

    def logg_from_age(self):
        """Estimate the surface gravity from model isochrones given an age and Lbol
        """
        if self.age is not None and self.Lbol_sun is not None:

            if self.Lbol_sun[1] is None:
                print('Lbol={0.Lbol}. Uncertainties are needed to calculate the surface gravity.'.format(self))
            else:
                try:
                    self.logg = self.evo_model.evaluate(self.Lbol_sun, self.age, 'Lbol', 'logg')
                except ValueError as err:
                    print("Could not calculate surface gravity.")
                    print(err)

        else:
            if self.verbose:
                print('Lbol={0.Lbol} and age={0.age}. Both are needed to calculate the surface gravity.'.format(self))

    def make_rj_tail(self, teff=3000*q.K):
        """Generate a Rayleigh Jeans tail for the SED

        Parameters
        ----------
        teff: astropy.units.quantity.Quantity
            The effective temperature of the source
        """
        # Make the blackbody from 2 to 1000um
        rj_wave = np.linspace(0.1, 1000, 2000)*q.um
        rj = sp.Blackbody(rj_wave, (teff, 100*q.K), name='RJ Tail')

        # Convert to native units
        rj.wave_units = self.wave_units
        rj.flux_units = self.flux_units

        # Normalize to longest wavelength data
        if self.max_spec > self.max_phot:
            rj = rj.norm_to_spec(self.app_spec_SED)
        else:
            rj = rj.norm_to_mags(self.photometry)

        # Trim so there is no data overlap
        max_wave = np.nanmax([self.max_spec.value, self.max_phot.value])
        rj.trim([(0*q.um, max_wave*self.wave_units)])

        self.rj = rj

    def make_sed(self):
        """Construct the SED"""
        # Make sure the is data
        if len(self.spectra) == 0 and len(self.photometry) == 0:
            raise ValueError('Cannot make the SED without spectra or photometry!')

        # Calculate flux and calibrate
        self._calibrate_photometry()

        # Combine spectra and flux calibrate
        self._calibrate_spectra()

        # Get synthetic mags
        # self.calculate_synthetic_mags()

        #
        if len(self.stitched_spectra) > 0:
            
            # If photometry and spectra, exclude photometric points with
            # spectrum coverage
            if len(self.photometry) > 0:
                covered = []
                for idx, i in enumerate(self.app_phot_SED.wave):
                    for N, spec in enumerate(self.stitched_spectra):
                        if i < spec.wave[-1] and i > spec.wave[0]:
                            covered.append(idx)
                WP, FP, EP = [[i for n, i in enumerate(A) if n not in covered]*Q for A, Q in zip(self.app_phot_SED.spectrum, self.units)]

                # If all the photometry is covered, just use spectra
                if len(WP) == 0:
                    self.app_specphot_SED = None
                else:
                    self.app_specphot_SED = sp.Spectrum(WP, FP, EP)

            # If no photometry, just use spectra
            else:
                self.app_specphot_SED = self.app_spec_SED

        # If no spectra, just use photometry
        else:
            self.app_specphot_SED = self.app_phot_SED

        # Make Wein and Rayleigh Jeans tails
        self.make_wein_tail()
        # self.make_rj_tail()
        self.rj = None

        # Run the calculation
        self._calculate_sed()

        # If Teff and Lbol have been caluclated, recalculate with 
        # better Blackbody
        if self.Teff_evo is not None:
            self.make_wein_tail(teff=self.Teff_evo[0])
            self.make_rj_tail(teff=self.Teff_evo[0])
            self._calculate_sed()

        # Set SED as calculated
        self.calculated = True

    def make_wein_tail(self, teff=None, trim=None):
        """Generate a Wein tail for the SED

        Parameters
        ----------
        teff: astropy.units.quantity.Quantity (optional)
            The effective temperature of the source
        """
        if teff is not None:

            # Make the blackbody from ~0 to 1um
            wein_wave = np.linspace(0.0001, 1.1, 500)*q.um
            wein = sp.Blackbody(wein_wave, (teff, 100*q.K), name='Wein Tail')

            # Convert to native units
            wein.wave_units = self.wave_units
            wein.flux_units = self.flux_units

            # Normalize to shortest wavelength data
            if self.min_spec < self.min_phot:
                wein = wein.norm_to_spec(self.app_spec_SED,
                                         exclude=[(1.1*q.um, 1E30*q.um)])
            else:
                wein = wein.norm_to_mags(self.photometry)

        else:

            # Otherwise just use ~0 flux at ~0 wavelength
            wein = sp.Spectrum(np.array([0.0001])*self.wave_units,
                               np.array([1E-30])*self.flux_units,
                               np.array([1E-30])*self.flux_units,
                               name='Wein Tail')

        # Trim so there is no data overlap
        min_wave = np.nanmin([self.min_spec.value, self.min_phot.value])
        wein.trim([(min_wave*self.wave_units, 1E30*q.um)])

        self.wein = wein

    def mass_from_age(self, mass_units=q.Msun):
        """Estimate the surface gravity from model isochrones given an age and Lbol
        """
        if self.age is not None and self.Lbol_sun is not None:

            if self.Lbol_sun[1] is None:
                print('Lbol={0.Lbol}. Uncertainties are needed to calculate the mass.'.format(self))
            else:
                try:
                    self.evo_model.mass_units = mass_units
                    self.mass = self.evo_model.evaluate(self.Lbol_sun, self.age, 'Lbol', 'mass')
                except ValueError as err:
                    print("Could not calculate mass.")
                    print(err)

        else:
            if self.verbose:
                print('Lbol={0.Lbol} and age={0.age}. Both are needed to calculate the mass.'.format(self))

    @property
    def membership(self):
        """A property for membership"""
        return self._membership

    @membership.setter
    def membership(self, membership):
        """A setter for membership"""
        if membership is None:

            self._membership = None

        elif membership in iso.NYMG_AGES:

            # Set the membership!
            self._membership = membership

            if self.verbose:
                print('Setting membership to', self.membership)

            # Set the age
            self.age = iso.NYMG_AGES.get(membership)

        else:
            print('{} not valid. Supported memberships include {}.'.format(membership, ', '.join(iso.NYMG_AGES.keys())))

    @property
    def name(self):
        """A property for name"""
        return self._name

    @name.setter
    def name(self, new_name):
        """A setter for the source name, which looks up metadata given a SIMBAD name

        Parameters
        ----------
        new_name: str
            The name
        """
        self._name = new_name

        # Check for sky coords
        self.find_SIMBAD()

    @property
    def parallax(self):
        """A property for parallax"""
        return self._parallax

    @parallax.setter
    def parallax(self, parallax, parallax_units=q.mas):
        """A setter for parallax

        Parameters
        ----------
        parallax: sequence
            The (parallax, err) or (parallax, lower_err, upper_err)
        """
        if parallax is None:

            self._parallax = None
            self._distance = None

            if self.isochrone_radius:
                self.radius = None
                self.isochrone_radius = False

        else:

            # Make sure it's a sequence
            typs = (tuple, list, np.ndarray)
            if not isinstance(parallax, typs) or len(parallax) not in [2, 3]:
                raise TypeError("""'parallax' must be a sequence of (value, error) \
                                   or (value, lower_error, upper_error).""")

            # Make sure the values are in time units
            if not parallax[0].unit.is_equivalent(q.mas):
                raise TypeError("""'parallax' values must be parallax units of \
                                   astropy.units.quantity.Quantity, e.g. 'mas'""")

            # Set the parallax
            self._parallax = parallax

            # Update the distance
            self._distance = u.pi2pc(*self.parallax)

        # Try to calculate reddening
        self.get_reddening()

        # Update the absolute photometry
        self._calibrate_photometry()

        # Update the flux calibrated spectra
        self._calibrate_spectra()

        # Set SED as uncalculated
        self.calculated = False

    @property
    def photometry(self):
        """A property for photometry"""
        self._photometry.sort('eff')
        return self._photometry

    def plot(self, app=True, photometry=True, spectra=True, integral=False,
             syn_photometry=True, blackbody=True, best_fit=True,
             scale=['log', 'log'], output=False, fig=None, color=None,
             **kwargs):
        """
        Plot the SED

        Parameters
        ----------
        app: bool
            Plot the apparent SED instead of absolute
        photometry: bool
            Plot the photometry
        spectra: bool
            Plot the spectra
        integrals: bool
            Plot the curve used to calculate fbol
        syn_photometry: bool
            Plot the synthetic photometry
        blackbody: bool
            Plot the blackbody fit
        best_fit: bool
            Plot the best fit model
        scale: array-like
            The (x, y) scales to plot, 'linear' or 'log'
        bokeh: bool
            Plot in Bokeh
        output: bool
            Just return figure, don't draw plot
        fig: bokeh.plotting.figure (optional)
            The Boheh plot to add the SED to
        color: str
            The color for the plot points and lines

        Returns
        -------
        bokeh.models.figure
            The SED plot
        """
        if not self.calculated:
            self.make_sed()

        # Distinguish between apparent and absolute magnitude
        pre = 'app_' if app else 'abs_'

        # Calculate reasonable axis limits
        full_SED = getattr(self, pre+'SED')
        spec_SED = getattr(self, pre+'spec_SED')
        phot_cols = ['eff', pre+'flux', pre+'flux_unc']
        phot_SED = np.array([np.array([np.nanmean(self.photometry.loc[b][col].value) for b in list(set(self.photometry['band']))]) for col in phot_cols])

        # Check for min and max phot data
        try:
            mn_xp = np.nanmin(phot_SED[0])
            mx_xp = np.nanmax(phot_SED[0])
            mn_yp = np.nanmin(phot_SED[1])
            mx_yp = np.nanmax(phot_SED[1])
        except:
            mn_xp, mx_xp, mn_yp, mx_yp = 0.3, 18, 0, 1

        # Check for min and max spec data
        try:
            mn_xs = np.nanmin(spec_SED.wave)
            mx_xs = np.nanmax(spec_SED.wave)
            mn_ys = np.nanmin(spec_SED.flux[spec_SED.flux>0])
            mx_ys = np.nanmax(spec_SED.flux[spec_SED.flux>0])
        except:
            mn_xs, mx_xs, mn_ys, mx_ys = 0.3, 18, 999, -999

        mn_x = np.nanmin([mn_xp, mn_xs])
        mx_x = np.nanmax([mx_xp, mx_xs])
        mn_y = np.nanmin([mn_yp, mn_ys])
        mx_y = np.nanmax([mx_yp, mx_ys])

        # Use input figure...
        if hasattr(fig, 'legend'):
            self.fig = fig

        # ...or make a new plot
        else:
            TOOLS = ['pan', 'reset', 'box_zoom', 'wheel_zoom', 'save']
            xlab = 'Wavelength [{}]'.format(self.wave_units)
            ylab = 'Flux Density [{}]'.format(str(self.flux_units))
            self.fig = figure(plot_width=800, plot_height=500, title=self.name,
                              y_axis_type=scale[1], x_axis_type=scale[0],
                              x_axis_label=xlab, y_axis_label=ylab,
                              tools=TOOLS)

        # Set the color
        if color is None:
            color = '#1f77b4'

        # Plot spectra
        if spectra and len(self.spectra) > 0:

            if spectra == 'all':
                for n, spec in enumerate(self.spectra):
                    self.fig = spec.plot(fig=self.fig, components=True)

            else:
                self.fig.line(spec_SED.wave, spec_SED.flux, color=color,
                              legend='Spectrum')

        # Plot photometry
        if photometry and self.photometry is not None:

            # Set up hover tool
            phot_tips = [( 'Band', '@desc'), ('Wave', '@x'), ( 'Flux', '@y'),
                         ('Unc', '@z')]
            hover = HoverTool(names=['photometry', 'nondetection'],
                              tooltips=phot_tips, mode='vline')
            self.fig.add_tools(hover)

            # Plot points with errors
            pts = np.array([(bnd, wav, flx, err) for bnd, wav, flx, err in np.array(self.photometry['band', 'eff', pre+'flux', pre+'flux_unc']) if not any([np.isnan(i) for i in [wav, flx, err]])], dtype=[('desc', 'S20'), ('x', float), ('y', float), ('z', float)])
            if len(pts) > 0:
                source = ColumnDataSource(data=dict(x=pts['x'], y=pts['y'],
                                          z=pts['z'],
                                          desc=[b.decode("utf-8") for b in pts['desc']]))
                self.fig.circle('x', 'y', source=source, legend='Photometry',
                                name='photometry', color=color, fill_alpha=0.7,
                                size=8)
                y_err_x = []
                y_err_y = []
                for name, px, py, err in pts:
                    y_err_x.append((px, px))
                    y_err_y.append((py - err, py + err))
                self.fig.multi_line(y_err_x, y_err_y, color=color)

            # Plot points without errors
            pts = np.array([(bnd, wav, flx, err) for bnd, wav, flx, err in np.array(self.photometry['band', 'eff', pre+'flux', pre+'flux_unc']) if np.isnan(err) and not np.isnan(flx)], dtype=[('desc', 'S20'), ('x', float), ('y', float), ('z', float)])
            if len(pts) > 0:
                source = ColumnDataSource(data=dict(x=pts['x'], y=pts['y'],
                                          z=pts['z'],
                                          desc=[str(b) for b in pts['desc']]))
                self.fig.circle('x', 'y', source=source, legend='Nondetection',
                                name='nondetection', color=color, fill_alpha=0,
                                size=8)

        # Plot synthetic photometry

        # Plot the SED with linear interpolation completion
        if integral:
            label = str(self.Teff[0]) if self.Teff is not None else 'Integral'
            self.fig.line(full_SED.wave, full_SED.flux, line_color='black',
                          alpha=0.3, legend=label)

        # Plot the blackbody fit
        if blackbody and self.blackbody:
            bb_wav, bb_flx = self.blackbody.data[:2]
            self.fig.line(bb_wav, bb_flx, line_color='red',
                          legend='{} K'.format(self.Teff_bb))

        if best_fit and len(self.best_fit) > 0:
            for bf in self.best_fit:
                # self.fig.line(bf.spectrum[0], bf.spectrum[1], legend=bf.name)
                self.fig.line(bf.spectrum[0], bf.spectrum[1], alpha=0.3,
                                         color=next(sp.COLORS),
                                         legend=bf.label)

        self.fig.legend.location = "top_right"
        self.fig.legend.click_policy = "hide"
        self.fig.x_range = Range1d(mn_x*0.8, mx_x*1.2)
        self.fig.y_range = Range1d(mn_y*0.5, mx_y*2)

        if not output:
            show(self.fig)

        return self.fig

    @property
    def ra(self):
        """A property for right ascension"""
        return self._ra

    @ra.setter
    def ra(self, ra, ra_unc=None, frame='icrs'):
        """Set the right ascension of the source

        Parameters
        ----------
        ra: astropy.units.quantity.Quantity
            The right ascension
        ra_unc: astropy.units.quantity.Quantity (optional)
            The uncertainty
        frame: str
            The reference frame
        """
        if not isinstance(ra, (q.quantity.Quantity, str)):
            raise TypeError("Cannot interpret ra :", ra)

        # Make sure it's decimal degrees
        self._ra = Angle(ra)
        if self.dec is not None:
            self.sky_coords = self.ra, self.dec

    @property
    def radius(self):
        """A property for radius"""
        return self._radius

    @radius.setter
    def radius(self, radius):
        """A setter for radius"""
        if radius is None:
            self._radius = None

        else:
            # Make sure it's a sequence
            if not isinstance(radius, (tuple, list, np.ndarray)) or len(radius) not in [2, 3]:
                raise TypeError('Radius must be a sequence of (value, error) or (value, lower_error, upper_error).')

            # Make sure the values are in length units
            if not radius[0].unit.is_equivalent(q.m):
                raise TypeError("Radius values must be length units of astropy.units.quantity.Quantity, e.g. 'Rjup'")

            # Set the radius!
            self._radius = radius

            if self.verbose:
                print('Setting radius to', self.radius)

        # Set SED as uncalculated
        self.calculated = False

    def radius_from_spectral_type(self, spt=None):
        """Estimate the radius from CMD plot

        Parameters
        ----------
        spt: float
            The spectral type float, where 0-99 correspond to types O0-Y9
        """
        spt = spt or self.spectral_type[0]
        try:
            self.radius = SptRadius.get_radius(spt)

        except:
            print("Could not estimate radius from spectral type {}".format(spt))

    def radius_from_age(self, radius_units=q.Rsun):
        """Estimate the radius from model isochrones given an age and Lbol
        """
        if self.age is not None and self.Lbol_sun is not None:

            try:
                self.evo_model.radius_units = radius_units
                self.radius = self.evo_model.evaluate(self.Lbol_sun, self.age, 'Lbol', 'radius')
                self.isochrone_radius = True

            except ValueError as err:
                print("Could not calculate radius.")
                print(err)

        else:
            if self.verbose:
                print('Lbol={0.Lbol} and age={0.age}. Both are needed to calculate the radius.'.format(self))

    @property
    def results(self):
        """A property for displaying the results"""
        # Make the SED to get the most recent results
        if not self.calculated:
            self.make_sed()

        # Get the results
        ptypes = (float, bytes, str, type(None), q.quantity.Quantity)
        params = {k[1:] if k.startswith('_') else k for k, v in
                  self.__dict__.items() if isinstance(v, ptypes) or
                  (isinstance(v, (list, tuple)) and len(v) == 2)}
        rows = []
        exclude = ['spectra']
        for param in sorted([p for p in params if p not in exclude]):

            # Get the values and format
            attr = getattr(self, param, None)

            if attr is None:
                attr = '--'

            if isinstance(attr, (tuple, list)):
                val, unc = attr[:2]
                unit = val.unit if hasattr(val, 'unit') else '--'
                val = val.value if hasattr(val, 'unit') else val
                unc = unc.value if hasattr(unc, 'unit') else unc
                if val < 1E-3 or val > 1e5:
                    val = float('{:.2e}'.format(val))
                    if unc is None:
                        unc = '--'
                    else:
                        unc = float('{:.2e}'.format(unc))
                rows.append([param, val, unc, unit])

            elif isinstance(attr, (str, float, bytes, int)):
                rows.append([param, attr, '--', '--'])

            else:
                pass

        return at.Table(np.asarray(rows), names=('param', 'value', 'unc', 'units'))

    @property
    def sky_coords(self):
        """A property for sky coordinates"""
        return self._sky_coords

    @sky_coords.setter
    def sky_coords(self, sky_coords, frame='icrs'):
        """A setter for sky coordinates

        Parameters
        ----------
        sky_coords: astropy.coordinates.SkyCoord, tuple
            The sky coordinates to use
        """
        # Make sure it's a sky coordinate
        if not isinstance(sky_coords, (SkyCoord, tuple)):
            raise TypeError('Sky coordinates must be astropy.coordinates.SkyCoord or (ra, dec) tuple.')

        if isinstance(sky_coords, tuple) and len(sky_coords) == 2:

            if isinstance(sky_coords[0], str):
                sky_coords = SkyCoord(ra=sky_coords[0], dec=sky_coords[1],
                                      unit=(q.hour, q.degree), frame=frame)

            elif isinstance(sky_coords[0], (float, Angle, q.quantity.Quantity)):
                sky_coords = SkyCoord(ra=sky_coords[0], dec=sky_coords[1],
                                      unit=q.degree, frame=frame)

            else:
                raise TypeError("Cannot convert type {} to coordinates.".format(type(sky_coords[0])))

        # Set the sky coordinates
        self._sky_coords = sky_coords
        self._ra = sky_coords.ra.degree
        self._dec = sky_coords.dec.degree

        # Try to calculate reddening
        self.get_reddening()

        # Try to find the source in Simbad
        self.find_SIMBAD()

    @property
    def spectra(self):
        """A property for spectra"""
        return self._spectra

    @property
    def spectral_type(self):
        """A property for spectral_type"""
        return self._spectral_type

    @spectral_type.setter
    def spectral_type(self, spectral_type, spectral_type_unc=None, gravity=None, lum_class=None, prefix=None):
        """A setter for spectral_type"""
        # Make sure it's a sequence
        if isinstance(spectral_type, str):
            self.SpT = spectral_type
            spec_type = u.specType(spectral_type)
            spectral_type, spectral_type_unc, prefix, gravity, lum_class = spec_type

        elif isinstance(spectral_type, tuple):
            spectral_type, spectral_type_unc, *other = spectral_type
            gravity = lum_class = prefix = ''
            if other:
                gravity, *other = other
            if other:
                lum_class, *other = other
            if other:
                prefix = other[0]

            self.SpT = u.specType([spectral_type, spectral_type_unc, prefix, gravity, lum_class or 'V'])

        else:
            raise TypeError('Please provide a string or tuple to set the spectral type.')

        # Set the spectral_type!
        self._spectral_type = spectral_type, spectral_type_unc or 0.5
        self.luminosity_class = lum_class or 'V'
        self.gravity = gravity or None
        self.prefix = prefix or None

        # Set the age if not explicitly set
        if self.age is None and self.gravity is not None:
            if gravity in ['b', 'beta', 'g', 'gamma']:
                self.age = 225*q.Myr, 75*q.Myr

            else:
                print("{} is an invalid gravity. Please use 'beta' or 'gamma' instead.".format(gravity))

        # If radius not explicitly set, estimate it from spectral type
        if self.spectral_type is not None and self.radius is None:
            self.radius_from_spectral_type()

        # Set SED as uncalculated
        self.calculated = False

    @property
    def synthetic_photometry(self):
        """A property for synthetic photometry"""
        self._synthetic_photometry.sort('eff')
        return self._synthetic_photometry

    def teff_from_age(self, teff_units=q.K):
        """Estimate the radius from model isochrones given an age and Lbol
        """
        if self.age is not None and self.Lbol_sun is not None:

            if self.Lbol_sun[1] is None:
                print('Lbol={0.Lbol}. Uncertainties are needed to calculate the Teff.'.format(self))
            else:
                try:
                    self.evo_model.teff_units = teff_units
                    self.Teff_evo = self.evo_model.evaluate(self.Lbol_sun, self.age, 'Lbol', 'teff')
                except ValueError as err:
                    print("Could not calculate Teff.")
                    print(err)

        else:
            if self.verbose:
                print('Lbol={0.Lbol} and age={0.age}. Both are needed to calculate the Teff.'.format(self))

    @property
    def wave_units(self):
        """A property for wave_units"""
        return self._wave_units

    @wave_units.setter
    def wave_units(self, wave_units):
        """A setter for wave_units

        Parameters
        ----------
        wave_units: astropy.units.quantity.Quantity
            The astropy units of the SED wavelength
        """
        # Make sure it's a quantity
        if not isinstance(wave_units, (q.core.PrefixUnit, q.core.Unit, q.core.CompositeUnit)):
            raise TypeError('wave_units must be astropy.units.quantity.Quantity')

        # Make sure the values are in length units
        try:
            wave_units.to(q.um)
        except:
            raise TypeError("wave_units must be a unit of length, e.g. 'um'")

        # Set the wave_units!
        self._wave_units = wave_units
        self.units = [self._wave_units, self._flux_units, self._flux_units]

        # Recalibrate the data
        self._calibrate_photometry()
        self._calibrate_spectra()

    # def get_syn_photometry(self, bands=[], plot=False):
    #     """
    #     Calculate the synthetic magnitudes
    #
    #     Parameters
    #     ----------
    #     bands: sequence
    #         The list of bands to calculate
    #     plot: bool
    #         Plot the synthetic mags
    #     """
    #     try:
    #         if not any(bands):
    #             bands = BANDPASSES['Band']
    #
    #         # Only get mags in regions with spectral coverage
    #         syn_mags = []
    #         for spec in [i.as_void() for i in self.piecewise]:
    #             spec = [Q*(i.value if hasattr(i, 'unit') else i) for i, Q in zip(spec, [self.wave_units, self.flux_units, self.flux_units])]
    #             syn_mags.append(s.all_mags(spec, bands=bands, plot=plot))
    #
    #         # Stack the tables
    #         self.syn_photometry = at.vstack(syn_mags)
    #
    #     except:
    #         print('No spectral coverage to calculate synthetic photometry.')


class VegaSED(SED):
    """A precomputed SED of Vega
    """
    def __init__(self, **kwargs):
        """Initialize the SED of Vega"""
        # Make the Spectrum object
        super().__init__(**kwargs)

        self.name = 'Vega'
        self.find_SDSS()
        self.find_2MASS()
        self.find_WISE()
        self.parallax = 130.23*q.mas, 0.36*q.mas
        self.radius = 2.818*q.Rsun, 0.008*q.Rsun
        self.spectral_type = 'A0'

        # Get the spectrum
        self.add_spectrum(sp.Vega())

        # Calculate
        self.make_sed()
