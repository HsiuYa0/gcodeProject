from svg_to_gcode.compiler import Compiler, interfaces

from .config import WorkMode


class MySmartCompiler(Compiler):
    def __init__(self, interface_class, movement_speed, cutting_speed, pass_depth, dwell_time=0, unit=None, custom_header=None, custom_footer=None):
        super().__init__(interface_class, movement_speed, cutting_speed, pass_depth, dwell_time, unit, custom_header, custom_footer)


class AdaptiveInterface(interfaces.Gcode):
    def __init__(self, mode=WorkMode.LASER, power=255, speed=1500, rpm=0):
        super().__init__()
        self.mode = mode
        self.power = power
        self.rpm = rpm
        self.set_laser_power(1)
        super().set_movement_speed(speed)
        self.precision = 4

    def header(self):
        return "G90 G21"

    def footer(self):
        return [self.laser_off(), f"G0 X0 Y0;"]
