import warnings
from gzip import GzipFile
from pathlib import Path

import numpy as np
from astropy import units as u
from astropy.coordinates import Angle
from astropy.time import Time
from eventio.file_types import is_eventio
from eventio.simtel.simtelfile import SimTelFile

from ctapipe.calib.camera.gainselection import ThresholdGainSelector
from ctapipe.containers import EventAndMonDataContainer
from ctapipe.core.traits import Bool, CaselessStrEnum
from ctapipe.instrument import (
    TelescopeDescription,
    SubarrayDescription,
    CameraDescription,
    CameraGeometry,
    CameraReadout,
    OpticsDescription,
)
from ctapipe.instrument.camera import UnknownPixelShapeWarning
from ctapipe.instrument.guess import guess_telescope, UNKNOWN_TELESCOPE
from ctapipe.io.eventsource import EventSource
from io import BufferedReader

__all__ = ["SimTelEventSource"]


def build_camera(cam_settings, pixel_settings, telescope):
    pixel_shape = cam_settings["pixel_shape"][0]
    try:
        pix_type, pix_rotation = CameraGeometry.simtel_shape_to_type(pixel_shape)
    except ValueError:
        warnings.warn(
            f"Unkown pixel_shape {pixel_shape} for camera_type {telescope.camera_name}",
            UnknownPixelShapeWarning,
        )
        pix_type = "hexagon"
        pix_rotation = "0d"

    geometry = CameraGeometry(
        telescope.camera_name,
        pix_id=np.arange(cam_settings["n_pixels"]),
        pix_x=u.Quantity(cam_settings["pixel_x"], u.m),
        pix_y=u.Quantity(cam_settings["pixel_y"], u.m),
        pix_area=u.Quantity(cam_settings["pixel_area"], u.m ** 2),
        pix_type=pix_type,
        pix_rotation=pix_rotation,
        cam_rotation=-Angle(cam_settings["cam_rot"], u.rad),
        apply_derotation=True,
    )
    readout = CameraReadout(
        telescope.camera_name,
        sampling_rate=u.Quantity(1 / pixel_settings["time_slice"], u.GHz),
        reference_pulse_shape=pixel_settings["ref_shape"].astype("float64", copy=False),
        reference_pulse_sample_width=u.Quantity(pixel_settings["ref_step"], u.ns),
    )

    return CameraDescription(
        camera_name=telescope.camera_name, geometry=geometry, readout=readout
    )


def apply_simtel_r1_calibration(r0_waveforms, pedestal, dc_to_pe, gain_selector):
    """
    Perform the R1 calibration for R0 simtel waveforms. This includes:
        - Gain selection
        - Pedestal subtraction
        - Conversion of samples into units proportional to photoelectrons
          (If the full signal in the waveform was integrated, then the resulting
          value would be in photoelectrons.)
          (Also applies flat-fielding)

    Parameters
    ----------
    r0_waveforms : ndarray
        Raw ADC waveforms from a simtel file. All gain channels available.
        Shape: (n_channels, n_pixels, n_samples)
    pedestal : ndarray
        Pedestal stored in the simtel file for each gain channel
        Shape: (n_channels, n_pixels)
    dc_to_pe : ndarray
        Conversion factor between R0 waveform samples and ~p.e., stored in the
        simtel file for each gain channel
        Shape: (n_channels, n_pixels)
    gain_selector : ctapipe.calib.camera.gainselection.GainSelector

    Returns
    -------
    r1_waveforms : ndarray
        Calibrated waveforms
        Shape: (n_pixels, n_samples)
    selected_gain_channel : ndarray
        The gain channel selected for each pixel
        Shape: (n_pixels)
    """
    n_channels, n_pixels, n_samples = r0_waveforms.shape
    ped = pedestal[..., np.newaxis] / n_samples
    gain = dc_to_pe[..., np.newaxis]
    r1_waveforms = (r0_waveforms - ped) * gain
    if n_channels == 1:
        selected_gain_channel = np.zeros(n_pixels, dtype=np.int8)
        r1_waveforms = r1_waveforms[0]
    else:
        selected_gain_channel = gain_selector(r0_waveforms)
        r1_waveforms = r1_waveforms[selected_gain_channel, np.arange(n_pixels)]
    return r1_waveforms, selected_gain_channel


class SimTelEventSource(EventSource):
    skip_calibration_events = Bool(True, help="Skip calibration events").tag(
        config=True
    )
    back_seekable = Bool(
        False,
        help=(
            "Require the event source to be backwards seekable."
            " This will reduce in slower read speed for gzipped files"
            " and is not possible for zstd compressed files"
        ),
    ).tag(config=True)

    focal_length_choice = CaselessStrEnum(
        ["nominal", "effective"],
        default_value="nominal",
        help=(
            "if both nominal and effective focal lengths are available in the "
            "SimTelArray file, which one to use when constructing the "
            "SubarrayDescription (which will be used in CameraFrame to TelescopeFrame "
            "coordinate transforms. The 'nominal' focal length is the one used during "
            "the simulation, the 'effective' focal length is computed using specialized "
            "ray-tracing from a point light source"
        ),
    ).tag(config=True)

    def __init__(self, config=None, parent=None, gain_selector=None, **kwargs):
        """
        EventSource for simtelarray files using the pyeventio library.

        Parameters
        ----------
        config : traitlets.loader.Config
            Configuration specified by config file or cmdline arguments.
            Used to set traitlet values.
            Set to None if no configuration to pass.
        tool : ctapipe.core.Tool
            Tool executable that is calling this component.
            Passes the correct logger to the component.
            Set to None if no Tool to pass.
        gain_selector : ctapipe.calib.camera.gainselection.GainSelector
            The GainSelector to use. If None, then ThresholdGainSelector will be used.
        kwargs
        """
        super().__init__(config=config, parent=parent, **kwargs)
        self.metadata["is_simulation"] = True
        self._camera_cache = {}

        # traitlets creates an empty set as default,
        # which ctapipe treats as no restriction on the telescopes
        # but eventio treats an emty set as "no telescopes allowed"
        # so we explicitly pass None in that case
        self.file_ = SimTelFile(
            Path(self.input_url).expanduser(),
            allowed_telescopes=set(self.allowed_tels) if self.allowed_tels else None,
            skip_calibration=self.skip_calibration_events,
            zcat=not self.back_seekable,
        )
        if self.back_seekable and self.is_stream:
            raise IOError("back seekable was required but not possible for inputfile")

        self._subarray_info = self.prepare_subarray_info(
            self.file_.telescope_descriptions, self.file_.header
        )
        self.start_pos = self.file_.tell()

        # Waveforms from simtelarray have both gain channels
        # Gain selection is performed by this EventSource to produce R1 waveforms
        if gain_selector is None:
            gain_selector = ThresholdGainSelector(parent=self)
        self.gain_selector = gain_selector

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def close(self):
        self.file_.close()

    @property
    def is_stream(self):
        return not isinstance(self.file_._filehandle, (BufferedReader, GzipFile))

    def prepare_subarray_info(self, telescope_descriptions, header):
        """
        Constructs a SubarrayDescription object from the
        ``telescope_descriptions`` given by ``SimTelFile``

        Parameters
        ----------
        telescope_descriptions: dict
            telescope descriptions as given by ``SimTelFile.telescope_descriptions``
        header: dict
            header as returned by ``SimTelFile.header``

        Returns
        -------
        SubarrayDescription :
            instrumental information
        """

        tel_descriptions = {}  # tel_id : TelescopeDescription
        tel_positions = {}  # tel_id : TelescopeDescription

        for tel_id, telescope_description in telescope_descriptions.items():
            cam_settings = telescope_description["camera_settings"]
            pixel_settings = telescope_description["pixel_settings"]

            n_pixels = cam_settings["n_pixels"]
            focal_length = u.Quantity(cam_settings["focal_length"], u.m)

            if self.focal_length_choice == "effective":
                try:
                    focal_length = u.Quantity(
                        cam_settings["effective_focal_length"], u.m
                    )
                except KeyError as err:
                    raise RuntimeError(
                        f"the SimTelEventSource option 'focal_length_choice' was set to "
                        f"{self.focal_length_choice}, but the effective focal length "
                        f"was not present in the file. ({err})"
                    )

            try:
                telescope = guess_telescope(n_pixels, focal_length)
            except ValueError:
                telescope = UNKNOWN_TELESCOPE

            camera = self._camera_cache.get(telescope.camera_name)
            if camera is None:
                camera = build_camera(cam_settings, pixel_settings, telescope)
                self._camera_cache[telescope.camera_name] = camera

            optics = OpticsDescription(
                name=telescope.name,
                num_mirrors=telescope.n_mirrors,
                equivalent_focal_length=focal_length,
                mirror_area=u.Quantity(cam_settings["mirror_area"], u.m ** 2),
                num_mirror_tiles=cam_settings["n_mirrors"],
            )

            tel_descriptions[tel_id] = TelescopeDescription(
                name=telescope.name,
                tel_type=telescope.type,
                optics=optics,
                camera=camera,
            )

            tel_idx = np.where(header["tel_id"] == tel_id)[0][0]
            tel_positions[tel_id] = header["tel_pos"][tel_idx] * u.m

        return SubarrayDescription(
            "MonteCarloArray",
            tel_positions=tel_positions,
            tel_descriptions=tel_descriptions,
        )

    @staticmethod
    def is_compatible(file_path):
        return is_eventio(Path(file_path).expanduser())

    @property
    def subarray(self):
        return self._subarray_info

    def _generator(self):
        if self.file_.tell() > self.start_pos:
            self.file_._next_header_pos = 0
            warnings.warn("Backseeking to start of file.")

        try:
            yield from self.__generator()
        except EOFError:
            msg = 'EOFError reading from "{input_url}". Might be truncated'.format(
                input_url=self.input_url
            )
            self.log.warning(msg)
            warnings.warn(msg)

    def __generator(self):
        data = EventAndMonDataContainer()
        data.meta["origin"] = "hessio"
        data.meta["input_url"] = self.input_url
        data.meta["max_events"] = self.max_events

        for counter, array_event in enumerate(self.file_):
            # next lines are just for debugging
            self.array_event = array_event
            data.event_type = array_event["type"]

            # calibration events do not have an event id
            if data.event_type == "calibration":
                event_id = -1
            else:
                event_id = array_event["event_id"]

            data.inst.subarray = self._subarray_info

            obs_id = self.file_.header["run"]
            tels_with_data = set(array_event["telescope_events"].keys())
            data.count = counter
            data.index.obs_id = obs_id
            data.index.event_id = event_id
            data.r0.obs_id = obs_id  # deprecated
            data.r0.event_id = event_id  # deprecated
            data.r0.tels_with_data = tels_with_data
            data.r1.obs_id = obs_id  # deprecated
            data.r1.event_id = event_id  # deprecated
            data.r1.tels_with_data = tels_with_data
            data.dl0.obs_id = obs_id  # deprecated
            data.dl0.event_id = event_id  # deprecated
            data.dl0.tels_with_data = tels_with_data

            # handle telescope filtering by taking the intersection of
            # tels_with_data and allowed_tels
            if len(self.allowed_tels) > 0:
                selected = tels_with_data & self.allowed_tels
                if len(selected) == 0:
                    continue  # skip event
                data.r0.tels_with_data = selected
                data.r1.tels_with_data = selected
                data.dl0.tels_with_data = selected

            trigger_information = array_event["trigger_information"]

            data.trig.tels_with_trigger = trigger_information["triggered_telescopes"]
            time_s, time_ns = trigger_information["gps_time"]
            data.trig.gps_time = Time(
                time_s * u.s, time_ns * u.ns, format="unix", scale="utc"
            )

            if data.event_type == "data":
                self.fill_mc_information(data, array_event)

            # this should be done in a nicer way to not re-allocate the
            # data each time (right now it's just deleted and garbage
            # collected)
            data.r0.tel.clear()
            data.r1.tel.clear()
            data.dl0.tel.clear()
            data.dl1.tel.clear()
            data.mc.tel.clear()  # clear the previous telescopes

            telescope_events = array_event["telescope_events"]
            tracking_positions = array_event["tracking_positions"]
            for tel_id, telescope_event in telescope_events.items():
                tel_index = self.file_.header["tel_id"].tolist().index(tel_id)

                adc_samples = telescope_event.get("adc_samples")
                if adc_samples is None:
                    adc_samples = telescope_event["adc_sums"][:, :, np.newaxis]
                _, n_pixels, n_samples = adc_samples.shape

                mc = data.mc.tel[tel_id]
                mc.dc_to_pe = array_event["laser_calibrations"][tel_id]["calib"]
                mc.pedestal = array_event["camera_monitorings"][tel_id]["pedestal"]
                mc.photo_electron_image = (
                    array_event.get("photoelectrons", {})
                    .get(tel_index, {})
                    .get("photoelectrons", np.zeros(n_pixels, dtype="float32"))
                )

                tracking_position = tracking_positions[tel_id]
                mc.azimuth_raw = tracking_position["azimuth_raw"]
                mc.altitude_raw = tracking_position["altitude_raw"]
                mc.azimuth_cor = tracking_position.get("azimuth_cor", np.nan)
                mc.altitude_cor = tracking_position.get("altitude_cor", np.nan)
                if np.isnan(mc.azimuth_cor):
                    data.pointing[tel_id].azimuth = u.Quantity(mc.azimuth_raw, u.rad)
                else:
                    data.pointing[tel_id].azimuth = u.Quantity(mc.azimuth_cor, u.rad)
                if np.isnan(mc.altitude_cor):
                    data.pointing[tel_id].altitude = u.Quantity(mc.altitude_raw, u.rad)
                else:
                    data.pointing[tel_id].altitude = u.Quantity(mc.altitude_cor, u.rad)

                r0 = data.r0.tel[tel_id]
                r1 = data.r1.tel[tel_id]
                r0.waveform = adc_samples
                r1.waveform, r1.selected_gain_channel = apply_simtel_r1_calibration(
                    adc_samples, mc.pedestal, mc.dc_to_pe, self.gain_selector
                )

                pixel_lists = telescope_event["pixel_lists"]
                r0.num_trig_pix = pixel_lists.get(0, {"pixels": 0})["pixels"]
                if r0.num_trig_pix > 0:
                    r0.trig_pix_id = pixel_lists[0]["pixel_list"]

            yield data

    def fill_mc_information(self, data, array_event):
        mc_event = array_event["mc_event"]
        mc_shower = array_event["mc_shower"]

        data.mc.energy = mc_shower["energy"] * u.TeV
        data.mc.alt = Angle(mc_shower["altitude"], u.rad)
        data.mc.az = Angle(mc_shower["azimuth"], u.rad)
        data.mc.core_x = mc_event["xcore"] * u.m
        data.mc.core_y = mc_event["ycore"] * u.m
        first_int = mc_shower["h_first_int"] * u.m
        data.mc.h_first_int = first_int
        data.mc.x_max = mc_shower["xmax"] * u.g / (u.cm ** 2)
        data.mc.shower_primary_id = mc_shower["primary_id"]

        # mc run header data
        data.mcheader.run_array_direction = Angle(
            self.file_.header["direction"] * u.rad
        )
        mc_run_head = self.file_.mc_run_headers[-1]
        data.mcheader.corsika_version = mc_run_head["shower_prog_vers"]
        data.mcheader.simtel_version = mc_run_head["detector_prog_vers"]
        data.mcheader.energy_range_min = mc_run_head["E_range"][0] * u.TeV
        data.mcheader.energy_range_max = mc_run_head["E_range"][1] * u.TeV
        data.mcheader.prod_site_B_total = mc_run_head["B_total"] * u.uT
        data.mcheader.prod_site_B_declination = Angle(
            mc_run_head["B_declination"] * u.rad
        )
        data.mcheader.prod_site_B_inclination = Angle(
            mc_run_head["B_inclination"] * u.rad
        )
        data.mcheader.prod_site_alt = mc_run_head["obsheight"] * u.m
        data.mcheader.spectral_index = mc_run_head["spectral_index"]
        data.mcheader.shower_prog_start = mc_run_head["shower_prog_start"]
        data.mcheader.shower_prog_id = mc_run_head["shower_prog_id"]
        data.mcheader.detector_prog_start = mc_run_head["detector_prog_start"]
        data.mcheader.detector_prog_id = mc_run_head["detector_prog_id"]
        data.mcheader.num_showers = mc_run_head["n_showers"]
        data.mcheader.shower_reuse = mc_run_head["n_use"]
        data.mcheader.max_alt = mc_run_head["alt_range"][1] * u.rad
        data.mcheader.min_alt = mc_run_head["alt_range"][0] * u.rad
        data.mcheader.max_az = mc_run_head["az_range"][1] * u.rad
        data.mcheader.min_az = mc_run_head["az_range"][0] * u.rad
        data.mcheader.diffuse = mc_run_head["diffuse"]
        data.mcheader.max_viewcone_radius = mc_run_head["viewcone"][1] * u.deg
        data.mcheader.min_viewcone_radius = mc_run_head["viewcone"][0] * u.deg
        data.mcheader.max_scatter_range = mc_run_head["core_range"][1] * u.m
        data.mcheader.min_scatter_range = mc_run_head["core_range"][0] * u.m
        data.mcheader.core_pos_mode = mc_run_head["core_pos_mode"]
        data.mcheader.injection_height = mc_run_head["injection_height"] * u.m
        data.mcheader.atmosphere = mc_run_head["atmosphere"]
        data.mcheader.corsika_iact_options = mc_run_head["corsika_iact_options"]
        data.mcheader.corsika_low_E_model = mc_run_head["corsika_low_E_model"]
        data.mcheader.corsika_high_E_model = mc_run_head["corsika_high_E_model"]
        data.mcheader.corsika_bunchsize = mc_run_head["corsika_bunchsize"]
        data.mcheader.corsika_wlen_min = mc_run_head["corsika_wlen_min"] * u.nm
        data.mcheader.corsika_wlen_max = mc_run_head["corsika_wlen_max"] * u.nm
        data.mcheader.corsika_low_E_detail = mc_run_head["corsika_low_E_detail"]
        data.mcheader.corsika_high_E_detail = mc_run_head["corsika_high_E_detail"]
