import unittest
from unittest.mock import Mock

from backend.video_acceleration import select_encoder, encoding_arguments, cpu_profile


class VideoAccelerationTests(unittest.TestCase):
    def test_gpu_requires_successful_real_initialization(self):
        probe=Mock(side_effect=[{'available':False,'reason':'driver missing'}, {'available':True,'reason':None}])
        profile,attempts=select_encoder('ffmpeg',1920,1080,30,probe=probe)
        self.assertEqual(profile['codec'],'h264_qsv')
        self.assertTrue(profile['hardware'])
        self.assertEqual([row['codec'] for row in attempts],['h264_nvenc','h264_qsv'])
        self.assertEqual(probe.call_args.args[2:5],(1920,1080,30))

    def test_unsupported_hardware_falls_back_to_cpu(self):
        probe=Mock(return_value={'available':False,'reason':'encoder not available'})
        profile,attempts=select_encoder('ffmpeg',1280,720,24,probe=probe)
        self.assertEqual(profile['codec'],'libx264')
        self.assertFalse(profile['hardware'])
        self.assertEqual(len(attempts),3)

    def test_cancelled_probe_does_not_start_a_cpu_retry(self):
        probe=Mock(side_effect=InterruptedError('cancelled'))
        with self.assertRaises(InterruptedError):
            select_encoder('ffmpeg',1280,720,24,probe=probe)
        self.assertEqual(probe.call_count,1)

    def test_cpu_compatibility_mode_skips_hardware_initialization(self):
        probe=Mock()
        profile,attempts=select_encoder('ffmpeg',640,360,12,mode='cpu',probe=probe)
        self.assertEqual(profile['codec'],'libx264')
        self.assertEqual(attempts,[])
        probe.assert_not_called()

    def test_unknown_mode_is_rejected(self):
        with self.assertRaisesRegex(ValueError,'VIDEO_ACCELERATION_MODE_INVALID'):
            select_encoder('ffmpeg',1280,720,24,mode='made-up')

    def test_acceleration_keeps_compatible_output_and_exact_frame_count(self):
        argv=encoding_arguments('ffmpeg',cpu_profile(),24,432,'video.partial.mp4')
        self.assertEqual(argv[argv.index('-frames:v')+1],'432')
        self.assertEqual(argv[argv.index('-pix_fmt')+1],'yuv420p')
        self.assertEqual(argv[argv.index('-c:v')+1],'libx264')
        self.assertEqual(argv[-1],'video.partial.mp4')


if __name__=='__main__':unittest.main()
