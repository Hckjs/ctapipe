import os
import sys
import pytest
import shlex
import matplotlib as mpl

from ctapipe.utils import get_dataset_path

GAMMA_TEST_LARGE = get_dataset_path("gamma_test_large.simtel.gz")


def test_muon_reconstruction(tmpdir):
    from ctapipe.tools.muon_reconstruction import MuonDisplayerTool
    return_code = MuonDisplayerTool().run(
        argv=shlex.split(
            f'--input={GAMMA_TEST_LARGE} '
            '--max_events=2 '
        )
    )
    assert return_code == 0


def test_display_summed_imaged(tmpdir):
    from ctapipe.tools.display_summed_images import ImageSumDisplayerTool
    mpl.use('Agg')
    return_code = ImageSumDisplayerTool().run(
        argv=shlex.split(
            f'--infile={GAMMA_TEST_LARGE} '
            '--max-events=2 '
        )
    )
    assert return_code == 0


def test_display_integrator(tmpdir):
    from ctapipe.tools.display_integrator import DisplayIntegrator
    mpl.use('Agg')
    return_code = DisplayIntegrator().run(
        argv=shlex.split(
            f'--f={GAMMA_TEST_LARGE} '
            '--max_events=1 '
        )
    )
    assert return_code == 0


def test_display_events_single_tel(tmpdir):
    from ctapipe.tools.display_events_single_tel import SingleTelEventDisplay
    mpl.use('Agg')
    return_code = SingleTelEventDisplay().run(
        argv=shlex.split(
            f'--infile={GAMMA_TEST_LARGE} '
            '--tel=11 '
            '--max-events=2 '  # <--- inconsistent!!!
        )
    )
    assert return_code == 0


def test_display_dl1(tmpdir):
    from ctapipe.tools.display_dl1 import DisplayDL1Calib
    mpl.use('Agg')
    return_code = DisplayDL1Calib().run(
        argv=shlex.split(
            '--max_events=1 '
            '--telescope=11 '
        )
    )
    assert return_code == 0


def test_info():
    from ctapipe.tools.info import info
    return_code = info(show_all=True)
    assert return_code == 0


def test_dump_triggers(tmpdir):
    from ctapipe.tools.dump_triggers import DumpTriggersTool

    sys.argv = ['dump_triggers']
    outfile = tmpdir.join("triggers.fits")

    tool = DumpTriggersTool(
        infile=GAMMA_TEST_LARGE,
        outfile=str(outfile)
    )

    return_code = tool.run(argv=[])
    assert return_code == 0
    assert outfile.exists()


def test_dump_instrument(tmpdir):
    from ctapipe.tools.dump_instrument import DumpInstrumentTool

    sys.argv = ['dump_instrument']
    tmpdir.chdir()

    tool = DumpInstrumentTool(
        infile=GAMMA_TEST_LARGE,
    )

    return_code = tool.run(argv=[])
    assert return_code == 0
    print(tmpdir.listdir())
    assert tmpdir.join('FlashCam.camgeom.fits.gz').exists()


def test_camdemo():
    from ctapipe.tools.camdemo import CameraDemo
    sys.argv = ['camera_demo']
    tool = CameraDemo()
    tool.num_events = 10
    tool.cleanframes = 2
    tool.display = False
    return_code = tool.run(argv=[])
    assert return_code == 0


def test_bokeh_file_viewer():
    from ctapipe.tools.bokeh.file_viewer import BokehFileViewer

    sys.argv = ['bokeh_file_viewer']
    tool = BokehFileViewer(disable_server=True)
    return_code = tool.run()
    assert return_code == 0
    assert tool.reader.input_url == get_dataset_path("gamma_test_large.simtel.gz")


def test_extract_charge_resolution(tmpdir):
    from ctapipe.tools.extract_charge_resolution import (
        ChargeResolutionGenerator
    )

    output_path = os.path.join(str(tmpdir), "cr.h5")
    tool = ChargeResolutionGenerator()
    with pytest.raises(KeyError):
        tool.run([
            '-f', GAMMA_TEST_LARGE,
            '-o', output_path,
        ])
    # TODO: Test files do not contain true charge, cannot test tool fully
    # assert os.path.exists(output_path)


def test_plot_charge_resolution(tmpdir):
    from ctapipe.tools.plot_charge_resolution import ChargeResolutionViewer
    from ctapipe.plotting.tests.test_charge_resolution import \
        create_temp_cr_file
    path = create_temp_cr_file(tmpdir)

    output_path = os.path.join(str(tmpdir), "cr.pdf")
    tool = ChargeResolutionViewer()
    return_code = tool.run([
        '-f', [path],
        '-o', output_path,
    ])
    assert return_code == 0
    assert os.path.exists(output_path)
