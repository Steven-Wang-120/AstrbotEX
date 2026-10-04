"""Pure checks for sensor provenance and observation accounting."""
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"sim"))
from mobile_manipulation.spec import SensorSpec,effective_config
spec=importlib.util.spec_from_file_location("capture",ROOT/"scripts/capture_mobile_sensors.py")
capture=importlib.util.module_from_spec(spec)
spec.loader.exec_module(capture)

class SensorContractsTests(unittest.TestCase):
    def test_native_sensor_dimension_changes_hash_but_not_camera(self):
        a=effective_config(SensorSpec(),"RGB_LIDAR_2D",101,"sensor_only")
        b=effective_config(SensorSpec(),"RGB_LIDAR_3D",101,"sensor_only")
        self.assertEqual(a["sensors"],b["sensors"])
        self.assertNotEqual(a["config_hash"],b["config_hash"])
        self.assertEqual(a["input_source"],"RAW_SENSOR")
        self.assertIsNone(a["perception_backend"])

    def test_nonintegral_publication_rate_is_rejected(self):
        with self.assertRaises(ValueError):
            SensorSpec(camera_hz=17).validate()

    def test_simulation_and_wall_rates_are_separate(self):
        rows=[{"stamp_ns":i*100000000,"received_monotonic_ns":i*200000000,"frame_id":"lidar"} for i in range(4)]
        result=capture.summary(rows)
        self.assertEqual(result["simulation_hz"],10)
        self.assertEqual(result["wall_hz"],5)

    def test_duplicate_sensor_stamp_is_not_hidden(self):
        row={"stamp_ns":100,"received_monotonic_ns":100,"frame_id":"lidar"}
        self.assertEqual(capture.summary([row,dict(row)])["non_increasing_stamps"],1)

    def test_png_preview_honors_padding_and_bgr_order(self):
        msg=SimpleNamespace(width=1,height=1,encoding="bgr8",step=4,data=bytes([1,2,3,255]))
        result=capture.image_png(msg)
        self.assertTrue(result.startswith(b"\\x89PNG".decode("unicode_escape").encode("latin1")))
        import struct,zlib
        offset=8
        while offset<len(result):
            length=struct.unpack("!I",result[offset:offset+4])[0]
            if result[offset+4:offset+8]==b"IDAT":
                self.assertEqual(zlib.decompress(result[offset+8:offset+8+length]),bytes([0,3,2,1]))
                break
            offset+=length+12
        else:
            self.fail("PNG has no image data")

if __name__=="__main__":
    unittest.main()
