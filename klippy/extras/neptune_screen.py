import logging
import time
from abc import ABC, abstractmethod

SERIAL_STATE_HEADER_NONE = 0
SERIAL_STATE_HEADER_ONE = 1
SERIAL_STATE_HEADER_TWO = 2
SERIAL_STATE_HEADER_MESSAGE = 3

SERIAL_HEADER_BYTE_1 = 0x5a
SERIAL_HEADER_BYTE_2 = 0xa5

DGUS_CMD_WRITEVAR = 0x82
DGUS_CMD_READVAR = 0x83

class NeptuneScreen:
    def __init__(self, config):        
        self._serial_state = None
        self._serial_state = SERIAL_STATE_HEADER_NONE
        self._axis_unit = 1
        self._temp_and_rate_unit = 1
        self._acceleration_unit = 100
        self._speed_ctrl = 'feedrate'
        self._temp_ctrl = 'extruder'
        self._last_message = None
        self._print_state = None
        self._zoffset_unit = 0.1
        self._gcode_callbacks = {}
        self._file_list = []
        self._requested_file = None
        self.printer = config.get_printer()
        self.config = config
        self.mutex = self.printer.get_reactor().mutex()
        self.name = config.get_name()
        self.reactor = self.printer.get_reactor()        
        self.heaters = []
        self.leds = []

        self.printer.register_event_handler("klippy:ready", self.handle_ready)       
        self.gcode = self.printer.lookup_object('gcode')

        uart = config.get('uart')
       
        self.variant = config.get('variant') or 'N3P'

        self.serial_bridge = self.printer.lookup_object(f'serial_bridge {uart}')
        self.serial_bridge.register_callback(self._handle_serial_bridge_response)

        self._update_interval = 2
        self._update_timer = self.reactor.register_timer(self._screen_update)                

    def _screen_update(self, eventtime):        
        stats = self.printer.lookup_object("print_stats").get_status(self.reactor.monotonic())

        for heater in self.heaters:            
            current_temp, target_temp = heater.get_temp(eventtime)            
            if(heater.name == 'heater_bed'):
                self.send_text("main.bedtemp.txt=\"" f'{current_temp:.0f} / {target_temp:.0f}' + "\"")
            else:
                self.send_text("main.nozzletemp.txt=\"" f'{current_temp:.0f} / {target_temp:.0f}' + "\"")
        
        if self._is_led_on(eventtime):
            self.send_text("status_led2=1")
        else:
            self.send_text("status_led2=0")

        g_status = self.printer.lookup_object("gcode_move").get_status()    

        self.send_text(f"printpause.zvalue.val={(g_status['position'].z * 10):.0f}")
        
        fan = self.printer.lookup_object("fan")
        self.send_text(f"printpause.fanspeed.txt=\"{(fan.get_status(eventtime)['speed'] * 100):.0f}%\"")

        last_state = self._print_state
        self._print_state = stats['state']

        if stats['state'] == 'printing' and last_state != stats['state']:
            self.send_text("page printpause")
        if stats['state'] == 'complete' and last_state != stats['state']:
            self.send_text("page main") 

        return eventtime + self._update_interval

    def _is_led_on(self, eventtime):
        for led in self.leds:
            status = led.get_status(eventtime)
            white = status["color_data"][0][3]
            
            if(white > 0):
                return True
            else:
                return False

    def _handle_serial_bridge_response(self, data):
        byte_debug = ' '.join(['0x{:02x}'.format(byte) for byte in data])
        self.log("Received message: " + byte_debug)
        messages = []
        message = self._last_message if self._last_message else None
        
        for byte in data:
            #self.log(f"Process data: state {self._serial_state} {message}")
            if self._serial_state == SERIAL_STATE_HEADER_NONE:
                if byte == SERIAL_HEADER_BYTE_1:
                    self._serial_state = SERIAL_STATE_HEADER_ONE
                else:
                    self._serial_state = SERIAL_STATE_HEADER_NONE
            elif self._serial_state == SERIAL_STATE_HEADER_ONE:
                if byte == SERIAL_HEADER_BYTE_2:
                    self._serial_state = SERIAL_STATE_HEADER_TWO
                else:
                    self._serial_state = SERIAL_STATE_HEADER_NONE
            elif self._serial_state == SERIAL_STATE_HEADER_TWO:
                self._serial_state = SERIAL_STATE_HEADER_MESSAGE
                message = Message()
                message.payload = []                
                message.length = byte
                self._last_message = message
            elif self._serial_state == SERIAL_STATE_HEADER_MESSAGE:                
                message.payload.append(byte)

                if(len(message.payload) == message.length):
                    messages.append(message)
                    message = None
                    self._last_message = None
                    self._serial_state = SERIAL_STATE_HEADER_NONE

        for message in messages:
            message.process_datagram()
            self.process_message(message)

    def process_message(self, message):
        self.log("Process message: " + str(message))

        move = self.printer.lookup_object("gcode_move")
        extrusion_factor = move.extrude_factor

        if(message.command == DGUS_CMD_READVAR):
            for Processor in CommandProcessors:
                Processor.process_if_match(message, self)

    def run_delayed_gcode(self, gcode, callback=None):
        self._gcode_callbacks[f'{time.time()}'] = {"gcode": gcode, "callback": callback}
        self.reactor.register_timer(self.gcode_command_timer, self.reactor.monotonic())
        
    def gcode_command_timer(self, eventtime):    
        with self.mutex:            
            for time in list(self._gcode_callbacks.keys()):
                command = self._gcode_callbacks[time]
                del self._gcode_callbacks[time]                
                code = command["gcode"]
                callback = command["callback"]

                self.log("Running delayed gcode: " + code)
                try:
                    self.gcode.run_script(code)
                    if callback:
                        callback()
                except Exception as e:
                    self.send_text("beep 2000")
                    self.log("Error running gcode script: " + str(e))

                self.log("Running delayed complete: " + code)
                
            return self.reactor.NEVER

    def _screen_init(self, eventtime):
        
        move = self.printer.lookup_object("gcode_move").get_status(self.reactor.monotonic())
        probe = self.printer.lookup_object("probe")

        self.send_text("page boot") 
        self.send_text("com_star") 
        self.send_text(f"main.va0.val={self._get_variant()}")        
        self.send_text("page main") 
        self.send_text(f"information.sversion.txt=\"Klipper\"")
        self.updateNumericVariable("restFlag1", f"1") #paused
        self.updateNumericVariable("restFlag2", f"1") #allow pause
        (x,y,z) = probe.get_offsets()
        homing_z = move['homing_origin'].z
        self.updateNumericVariable("leveldata.z_offset.val", f"{((homing_z - z) * 100):.0f}")

        self.reactor.update_timer(self._update_timer, eventtime + self._update_interval)
        return self.reactor.NEVER

    def updateTextVariable(self, key, value):
        self.send_text(f"{key}=\"{value}\"")

    def updateNumericVariable(self, key, value):
        self.send_text(f"{key}={value}")

    def _get_variant(self):
        if self.variant == "3Pro":
            return 1
        elif self.variant == "3Max":
            return 3
        elif self.variant == "3Plus":
            return 2
        else:
            return 1

    def get_estimated_print_time(self):
        stats = self.printer.lookup_object("print_stats").get_status(self.reactor.monotonic())
        sd = self.printer.lookup_object("virtual_sdcard").get_status(self.reactor.monotonic())

        return (stats['print_duration'] / sd['progress']) if sd['progress'] > 0 else 0

    def handle_ready(self):
        self.log("Ready")
        pheaters = self.printer.lookup_object('heaters')
        self.printer.load_object(self.config, 'heaters')
        heater_names = self.config.getlist("heater", ("extruder", "heater_bed"))

        self.heaters = [pheaters.lookup_heater(n) for n in heater_names]

        self.reactor.register_timer(self._reset_screen, self.reactor.monotonic())        

        pled = self.printer.lookup_object("led")
        self.leds =  [pled.led_helpers.get(n) for n in pled.led_helpers.keys() ]

        #for n in self.printer.lookup_objects():
        #    self.log(f"object: {n}" )
        
    def send_text(self, text):
        self.serial_bridge.send_text(text)

    def log(self, msg, *args, **kwargs):
        logging.info("Neptune Screen: " + str(msg))

    def _reset_screen(self, eventtime):
        self.log("Reset")
        self.send_text("com_star")
        self.send_text("rest")
        self.reactor.register_timer(self._screen_init, self.reactor.monotonic() + 2.)
        return self.reactor.NEVER
    
def load_config(config):
    return NeptuneScreen(config)

class Message:
    def __init__(self):
        self.command = None
        self.payload = []
        self.length = None
        self.command_data_length = None
        self.command_data = None
        self.command_address = None

    def process_datagram(self):
        self.command = self.payload[0]
        self.command_address = ((self.payload[1] & 0xff) << 8) | (self.payload[2] & 0xff)
        self.command_data_length = self.payload[3]

        self.command_data = []
        it = iter(self.payload[4:])
        for byte in it:
            self.command_data.append(((byte & 0xff) << 8) | (next(it) & 0xff))

    def __str__(self):
        payload_str = ' '.join([f'0x{byte:02x}' for byte in self.payload])
        return f'payload: { payload_str }, length: {self.length}, command: 0x{self.command:02x}, command_address: 0x{self.command_address:04x} command_data_length: {self.command_data_length}, command_data: {self.command_data}'

DGUS_KEY_MAIN_PAGE = 0x1002
DGUS_KEY_STOP_PRINT = 0x1008
DGUS_KEY_PAUSE_PRINT = 0x100A
DGUS_KEY_RESUME_PRINT = 0x100C
DGUS_KEY_ADJUSTMENT = 0x1004
DGUS_KEY_TEMP_SCREEN = 0x1030
DGUS_KEY_SETTING_BACK_KEY = 0x1040
DGUS_KEY_COOL_SCREEN = 0x1032
DGUS_KEY_HEATER0_TEMP_ENTER = 0x1034
DGUS_KEY_HOTBED_TEMP_ENTER = 0x103A
DGUS_KEY_SETTING_SCREEN = 0x103E
DGUS_KEY_BED_LEVEL = 0x1044
DGUS_KEY_AXIS_PAGE_SELECT = 0x1046
DGUS_KEY_XAXIS_MOVE_KEY = 0x1048
DGUS_KEY_YAXIS_MOVE_KEY = 0x104A
DGUS_KEY_ZAXIS_MOVE_KEY = 0x104C
DGUS_KEY_HARDWARE_TEST = 0x2202
DGUS_KEY_PRINT_FILE = 0x2198
DGUS_KEY_SELECT_FILE = 0x2199
DGUS_KEY_POWER_CONTINUE = 0x105f

class CommandProcessor(ABC):
    def __init__(self, address, command=None):
        self.address = address
        self.command = command
        pass

    def is_match(self, message):
        return message.command_address == self.address and (self.command is None or self.command == message.command_data[0])

    def process_if_match(self, message, screen):
        if self.is_match(message):
            self.process(message, screen)

    @abstractmethod
    def process(self, data, screen):
        pass
        
class MainPageProcessor(CommandProcessor):
    def process(self, message, screen):
        if message.command_data[0] == 0x1: #print button
            status = screen.printer.lookup_object("print_stats").get_status(screen.reactor.monotonic())
            sd = screen.printer.lookup_object("virtual_sdcard")

            if status['state'] in ['printing', 'paused']:
                screen.send_text("page printpause")
            else:
                screen.send_text("page file1")

                limit = 25
                screen._file_list = sd.get_file_list()            
                index = 0
                for fname, fsize in screen._file_list:
                    if(index <= limit):
                        screen.log(F"Sending file {fname}")
                        page = ((index // 5) + 1)
                        screen.updateTextVariable(f"file{page}.t{index}.txt", fname)
                        index+=1
            #screen.send_text("page printpause")

class BedLevelProcessor(CommandProcessor):
    def process(self, message, screen):
        if message.command_data[0] == 0x2 or message.command_data[0] == 0x3: #z-offset up/down
            move = screen.printer.lookup_object("gcode_move").get_status(screen.reactor.monotonic())
            probe = screen.printer.lookup_object("probe")

            unit = screen._zoffset_unit
            if message.command_data[0] == 0x3:
                unit *= -1           
            
            (x,y,z) = probe.get_offsets()

            homing_z = move['homing_origin'].z
            new_offset = homing_z + unit

            screen.run_delayed_gcode(f"SET_GCODE_OFFSET Z={new_offset} MOVE=1")

            screen.updateNumericVariable("leveldata.z_offset.val", f"{((new_offset - z) * 100):.0f}")
            screen.updateNumericVariable("adjustzoffset.z_offset.val", f"{((new_offset - z) * 100):.0f}")        
        if message.command_data[0] == 0x4: #z-offset unit 0.01
            screen._zoffset_unit = 0.01
            screen.updateNumericVariable("adjustzoffset.zoffset_value.val", f'{1}')
        if message.command_data[0] == 0x5: #z-offset unit 0.1
            screen._zoffset_unit = 0.1
            screen.updateNumericVariable("adjustzoffset.zoffset_value.val", f'{2}')
        if message.command_data[0] == 0x6: #z-offset unit 1
            screen._zoffset_unit = 1
            screen.updateNumericVariable("adjustzoffset.zoffset_value.val", f'{3}')
        if message.command_data[0] == 0x8: #light button
            pled = screen.printer.lookup_object("led")
            for n in pled.led_helpers.keys():
                status = pled.led_helpers[n].get_status(None)
                white = status["color_data"][0][3]
                
                if(white > 0):
                    screen.run_delayed_gcode(f"SET_LED LED={n} WHITE=0")
                else:
                    screen.run_delayed_gcode(f"SET_LED LED={n} WHITE=1")    
        if message.command_data[0] == 0x9: #bed level calibrate
            screen.run_delayed_gcode("M140 S60\nM104 S140\nM109 S140\nM190 S60\nBED_MESH_CALIBRATE SAMPLES=2\nG28\nG1 F200 Z0", lambda: (
                screen.send_text("page leveldata_36"),
                screen.send_text("page warn_zoffset")
            ))
        if message.command_data[0] == 0xa: #print pause request status
            move = screen.printer.lookup_object("gcode_move").get_status(screen.reactor.monotonic())
            stats = screen.printer.lookup_object("print_stats").get_status(screen.reactor.monotonic())
            sd = screen.printer.lookup_object("virtual_sdcard").get_status(screen.reactor.monotonic())
            fan = screen.printer.lookup_object("fan")

            estimated_time_left = screen.get_estimated_print_time()

            screen.updateTextVariable("printpause.t0.txt", f"{stats['filename']}")
            screen.updateNumericVariable("printpause.printprocess.val", f"{(sd['progress'] * 100):.0f}")
            screen.updateTextVariable("printpause.printvalue.txt", f"{(sd['progress'] * 100):.0f}")
            screen.updateTextVariable("printpause.printtime.txt", f"{(stats['print_duration'] / 60.0):.0f} / {(estimated_time_left / 60):.0f} min")
            
            screen.updateTextVariable("printpause.fanspeed.txt", f"{(fan.get_status(screen.reactor.monotonic())['speed'] * 100):.0f}%")

        if message.command_data[0] == 0x16: #print initial request
            move = screen.printer.lookup_object("gcode_move").get_status(screen.reactor.monotonic())
            stats = screen.printer.lookup_object("print_stats").get_status(screen.reactor.monotonic())
            sd = screen.printer.lookup_object("virtual_sdcard").get_status(screen.reactor.monotonic())

            estimated_time_left = screen.get_estimated_print_time()

            screen.updateTextVariable("printpause.printspeed.txt", f"{(move['speed_factor'] * 100):.0f}")
            screen.updateTextVariable("printpause.printtime.txt", f"{(stats['print_duration'] / 60.0):.0f} / {(estimated_time_left / 60):.0f} min")

            screen.updateNumericVariable("printpause.printprocess.val", f"{(sd['progress'] * 100):.0f}")
            screen.updateTextVariable("printpause.printvalue.txt", f"{(sd['progress'] * 100):.0f}")
            
            #restFlag1: 0 - printing, 1- paused
            #restFlag2: m76 pauses print timer setting this to 0 and restflag to 1, 1 --abort sd, 1 when hotend temp reached
            #can only pause print when restflag2=1
            
            if stats['state'] == 'printing':
                screen.updateNumericVariable("restFlag1", f"0")
            else:
                screen.updateNumericVariable("restFlag1", f"1")            

class AdjustmentProcessor(CommandProcessor):
    def process(self, message, screen):    
        if message.command_data[0] == 0x1:
            screen._temp_and_rate_unit = 10
            screen._temp_ctrl = 'extruder'
            heater = screen.printer.lookup_object('heaters').lookup_heater(screen._temp_ctrl)
            (current_temp, target_temp) = heater.get_temp(screen.reactor.monotonic())            
            screen.updateNumericVariable("adjusttemp.targettemp.val", f'{target_temp:.0f}')  
        if message.command_data[0] == 0x02:
            screen.send_text("page printpause")
        if message.command_data[0] == 0x05:
            screen._temp_and_rate_unit = 10
            screen.send_text("page adjusttemp")
        if message.command_data[0] == 0x06: #speed adjustment page
            screen._temp_and_rate_unit = 10
            screen._speed_ctrl = 'feedrate'

            move = screen.printer.lookup_object("gcode_move").get_status(screen.reactor.monotonic())

            screen.updateNumericVariable("adjustspeed.targetspeed.val", f"{(move['speed_factor'] * 100):.0f}")  
            screen.send_text("page adjustspeed")
        if message.command_data[0] == 0x07: #adjust screen button
            move = screen.printer.lookup_object("gcode_move").get_status(screen.reactor.monotonic())
            probe = screen.printer.lookup_object("probe")

            screen._zoffset_unit = 0.1            
            (x,y,z) = probe.get_offsets()

            homing_z = move['homing_origin'].z

            screen.updateNumericVariable("adjustzoffset.zoffset_value.val", f'{2}')
            screen.updateNumericVariable("adjustzoffset.z_offset.val", f"{(((homing_z - z)) * 100):.0f}")
            screen.send_text("page adjustzoffset")

        if message.command_data[0] == 0x08: #reset target feedrate
            screen.run_delayed_gcode("M220 S100")        
            screen.updateNumericVariable("adjustspeed.targetspeed.val", f"{(100):.0f}")      
        if message.command_data[0] == 0x09: #reset target flow
            screen.run_delayed_gcode("M221 S100")
            screen.updateNumericVariable("adjustspeed.targetspeed.val", f"{(100):.0f}")  
        if message.command_data[0] == 0x0A: #reset target fanspeed - 100%
            screen.run_delayed_gcode("M106 S255")
            screen.updateNumericVariable("adjustspeed.targetspeed.val", f"{(100):.0f}")  

class TempScreenProcessor(CommandProcessor):
    def process(self, message, screen):
        if message.command_data[0] == 0x1: #get hotend temp
            screen._temp_ctrl = 'extruder'
            heater = screen.printer.lookup_object('heaters').lookup_heater(screen._temp_ctrl)
            (current_temp, target_temp) = heater.get_temp(screen.reactor.monotonic())            
            screen.updateNumericVariable("adjusttemp.targettemp.val", f'{target_temp:.0f}')
        if message.command_data[0] == 0x3: #get bed temp
            screen._temp_ctrl = 'heater_bed'
            heater = screen.printer.lookup_object('heaters').lookup_heater(screen._temp_ctrl)
            (current_temp, target_temp) = heater.get_temp(screen.reactor.monotonic())            
            screen.updateNumericVariable("adjusttemp.targettemp.val", f'{target_temp:.0f}')
        if message.command_data[0] == 0x5: #.1mm
            screen._axis_unit = 0.1
            screen._temp_and_rate_unit = 1
            screen._acceleration_unit = 10
        if message.command_data[0] == 0x6: #1mm
            screen._axis_unit = 1.0
            screen._temp_and_rate_unit = 5
            screen._acceleration_unit = 50
        if message.command_data[0] == 0x7: #10mm
            screen._axis_unit = 10.0
            screen._temp_and_rate_unit = 10
            screen._acceleration_unit = 100
        if message.command_data[0] == 0x8 or message.command_data[0] == 0x9: #increase hotend temp by temp_unit            
            heater = screen.printer.lookup_object('heaters').lookup_heater(screen._temp_ctrl)
            (current_temp, target_temp) = heater.get_temp(screen.reactor.monotonic())

            min_temp = 25
            max_temp = 0
            if screen._temp_ctrl == 'extruder':                
                max_temp = 230
            elif screen._temp_ctrl == 'heater_bed':
                max_temp = 125

            new_target_temp = target_temp + screen._temp_and_rate_unit * (1 if message.command_data[0] == 0x8 else - 1)
            if(new_target_temp >= min_temp and new_target_temp <= max_temp):
                gcode = ('M104' if screen._temp_ctrl == 'extruder' else 'M140')
                screen.run_delayed_gcode(F"{gcode} S{new_target_temp}")
                screen.updateNumericVariable("adjusttemp.targettemp.val", f'{new_target_temp:.0f}')
        
        if message.command_data[0] == 0xA: #speed rate get
            screen._speed_ctrl = 'feedrate'
            move = screen.printer.lookup_object("gcode_move").get_status(screen.reactor.monotonic())
            screen.updateNumericVariable("adjustspeed.targetspeed.val", f"{(move['speed_factor'] * 100):.0f}")  
        if message.command_data[0] == 0xB: #flow control get
            screen._speed_ctrl = 'flowrate'
            move = screen.printer.lookup_object("gcode_move").get_status(screen.reactor.monotonic())
            screen.updateNumericVariable("adjustspeed.targetspeed.val", f"{(move['extrude_factor'] * 100):.0f}")  
        if message.command_data[0] == 0xC: #fan speed get
            screen._speed_ctrl = 'fanspeed'
            fan = screen.printer.lookup_object("fan")
            screen.updateNumericVariable("adjustspeed.targetspeed.val", f"{(fan.get_status(screen.reactor.monotonic())['speed'] * 100):.0f}")  
        if message.command_data[0] == 0xD or message.command_data[0] == 0xE: #increase/decrease rate        
            unit = screen._temp_and_rate_unit

            if message.command_data[0] == 0xE: #increase
                unit *= -1

            if(screen._speed_ctrl == 'feedrate'):
                move = screen.printer.lookup_object("gcode_move").get_status(screen.reactor.monotonic())
                            
                new_rate = (move['speed_factor'] + (unit / 100.0)) * 100

                min_rate = 0
                if new_rate < min_rate:
                    new_rate = min_rate

                screen.run_delayed_gcode(f"M220 S{new_rate:.0f}")
                screen.updateNumericVariable("adjustspeed.targetspeed.val", f"{(new_rate):.0f}")  
            if(screen._speed_ctrl == 'flowrate'):
                move = screen.printer.lookup_object("gcode_move").get_status(screen.reactor.monotonic())
                new_rate = (move['extrude_factor'] + (unit / 100.0)) * 100

                max_rate = 150                                              
                if new_rate > max_rate:
                    new_rate = max_rate

                min_rate = 0
                if new_rate < min_rate:
                    new_rate = min_rate

                screen.run_delayed_gcode(f"M221 S{new_rate:.0f}")
                screen.updateNumericVariable("adjustspeed.targetspeed.val", f"{(new_rate):.0f}")  
            if(screen._speed_ctrl == 'fanspeed'):
                fan = screen.printer.lookup_object("fan")
                new_rate = fan.get_status(screen.reactor.monotonic())['speed'] + (unit / 100.0)

                max_rate = 1                                            
                if new_rate > max_rate:
                    new_rate = max_rate

                min_rate = 0
                if new_rate < min_rate:
                    new_rate = min_rate

                screen.run_delayed_gcode(f"M106 S{(new_rate * 255.0):.0f}")
                screen.updateNumericVariable("adjustspeed.targetspeed.val", f"{(new_rate * 100):.0f}")  
        if message.command_data[0] in [0x10, 0x0f]: #speed setting page, acceleration page
            screen._acceleration_unit = 100
            toolhead = screen.printer.lookup_object("toolhead").get_status(screen.reactor.monotonic())
            #speedsetvalue.t0.txt - x
            #speedsetvalue.t1.txt - y
            #speedsetvalue.t2.txt - z
            #speedsetvalue.t3.txt - e
            #speesetvalue.xaxis.val
            #speesetvalue.yaxis.val
            #speesetvalue.zaxis.val
            #speesetvalue.eaxis.val
            screen.updateTextVariable("speedsetvalue.t0.txt", "Accel.")
            screen.updateTextVariable("speedsetvalue.t1.txt", "Max Accel. to Decel.")
            screen.updateTextVariable("speedsetvalue.t2.txt", "SCV")
            screen.updateTextVariable("speedsetvalue.t3.txt", "Velocity")
            screen.updateNumericVariable("speedsetvalue.xaxis.val", f'{toolhead["max_accel"]:.0f}')
            screen.updateNumericVariable("speedsetvalue.yaxis.val", f'{toolhead["max_accel_to_decel"]:.0f}')
            screen.updateNumericVariable("speedsetvalue.zaxis.val", f'{toolhead["square_corner_velocity"]:.0f}')
            screen.updateNumericVariable("speedsetvalue.eaxis.val", f'{toolhead["max_velocity"]:.0f}')
        if message.command_data[0] in [0x11, 0x12, 0x13, 0x14, 0x15, 0x16, 0x17, 0x18]: #axis acceleration down (0x11 - 0x14) / up (0x15, - 0x18)
            toolhead = screen.printer.lookup_object("toolhead")
            status = toolhead.get_status(screen.reactor.monotonic())
            unit = screen._acceleration_unit

            if message.command_data[0] == 0x11: #accel -                
                screen.run_delayed_gcode(f"SET_VELOCITY_LIMIT ACCEL={(status['max_accel'] - unit):.0f}")
                screen.updateNumericVariable("speedsetvalue.xaxis.val", f"{(status['max_accel'] - unit):.0f}")
            if message.command_data[0] == 0x12: #decel -
                screen.run_delayed_gcode(f"SET_VELOCITY_LIMIT ACCEL_TO_DECEL={(status['max_accel_to_decel'] - unit):.0f}")
                screen.updateNumericVariable("speedsetvalue.yaxis.val", f"{(status['max_accel_to_decel'] - unit):.0f}")
            if message.command_data[0] == 0x13: #scv -
                unit = unit // 10
                screen.run_delayed_gcode(f"SET_VELOCITY_LIMIT SQUARE_CORNER_VELOCITY={(status['square_corner_velocity'] - unit):.0f}")
                screen.updateNumericVariable("speedsetvalue.zaxis.val", f"{(status['square_corner_velocity'] - unit):.0f}")
            if message.command_data[0] == 0x14: #velocity -
                screen.run_delayed_gcode(f"SET_VELOCITY_LIMIT VELOCITY={(status['max_velocity'] - unit):.0f}")
                screen.updateNumericVariable("speedsetvalue.eaxis.val", f"{(status['max_velocity'] - unit):.0f}")
            if message.command_data[0] == 0x15: #accel +
                screen.run_delayed_gcode(f"SET_VELOCITY_LIMIT ACCEL={(status['max_accel'] + unit):.0f}")
                screen.updateNumericVariable("speedsetvalue.xaxis.val", f"{(status['max_accel'] + unit):.0f}")
            if message.command_data[0] == 0x16: #decel +
                screen.run_delayed_gcode(f"SET_VELOCITY_LIMIT ACCEL_TO_DECEL={(status['max_accel_to_decel'] + unit):.0f}")
                screen.updateNumericVariable("speedsetvalue.yaxis.val", f"{(status['max_accel_to_decel'] + unit):.0f}")
            if message.command_data[0] == 0x17: #scv +
                unit = unit // 10
                screen.run_delayed_gcode(f"SET_VELOCITY_LIMIT SQUARE_CORNER_VELOCITY={(status['square_corner_velocity'] + unit):.0f}")
                screen.updateNumericVariable("speedsetvalue.zaxis.val", f"{(status['square_corner_velocity'] + unit):.0f}")
            if message.command_data[0] == 0x18: #velocity +
                screen.run_delayed_gcode(f"SET_VELOCITY_LIMIT VELOCITY={(status['max_velocity'] + unit):.0f}")
                screen.updateNumericVariable("speedsetvalue.eaxis.val", f"{(status['max_velocity'] + unit):.0f}")

class CoolScreenProcessor(CommandProcessor):
    def process(self, message, screen):
        if message.command_data[0] == 1: #power off hotend
            screen.run_delayed_gcode("M104 S0")
        if message.command_data[0] == 2: #power off bed
            screen.run_delayed_gcode("M140 S0")
        if message.command_data[0] == 13: #pla temp
            screen._temp_and_rate_unit = 10
        if message.command_data[0] == 14: #petg temp
            screen._temp_and_rate_unit = 10
        if message.command_data[0] == 15: #abs temp
            screen._temp_and_rate_unit = 10
        if message.command_data[0] == 16: #tpu temp
            screen._temp_and_rate_unit = 10
        if message.command_data[0] == 17:
            screen._temp_and_rate_unit = 10

class AxisPageSelectProcessor(CommandProcessor):
    def process(self, message, screen):
        if message.command_data[0] == 1:
            screen._axis_unit = 0.1
        elif message.command_data[0] == 2:
            screen._axis_unit = 1.0
        elif message.command_data[0] == 3:
            screen._axis_unit = 10
        elif message.command_data[0] == 4:
            screen.send_text("page autohome")
            screen.run_delayed_gcode("G28", lambda:(
                screen.send_text("page premove")
            ))
        elif message.command_data[0] == 5:
            screen.send_text("page autohome")
            screen.run_delayed_gcode("G28 X", lambda:(
                screen.send_text("page premove")
            ))            
        elif message.command_data[0] == 6:
            screen.send_text("page autohome")
            screen.run_delayed_gcode("G28 Y", lambda:(
                screen.send_text("page premove")
            ))            
        elif message.command_data[0] == 7:                  
            screen.send_text("page autohome")
            screen.run_delayed_gcode("G28 Z", lambda:(
                screen.send_text("page premove")
            ))

class ZAxisMoveKeyProcessor(CommandProcessor):
    def process(self, message, screen):
        move = screen.printer.lookup_object("gcode_move")
        current_z = move.get_status()["gcode_position"].z
        if message.command_data[0] == 0x01:
            if move.absolute_coord:
                screen.run_delayed_gcode(F"G0 Z{(current_z + screen._axis_unit)}")
            else:
                screen.run_delayed_gcode(F"G0 Z+{screen._axis_unit}")
        else:
            if move.absolute_coord:
                screen.run_delayed_gcode(F"G0 Z{(current_z - screen._axis_unit)}")
            else:
                screen.run_delayed_gcode(F"G0 Z-{screen._axis_unit}")

class YAxisMoveKeyProcessor(CommandProcessor):
    def process(self, message, screen):
        move = screen.printer.lookup_object("gcode_move")
        current_y = move.get_status()["gcode_position"].y
        if message.command_data[0] == 0x01:
            if move.absolute_coord:
                screen.run_delayed_gcode(F"G0 Y{(current_y + screen._axis_unit)}")
            else:
                screen.run_delayed_gcode(F"G0 Y+{screen._axis_unit}")
        else:
            if move.absolute_coord:
                screen.run_delayed_gcode(F"G0 Y{(current_y - screen._axis_unit)}")
            else:
                screen.run_delayed_gcode(F"G0 Y-{screen._axis_unit}")

class XAxisMoveKeyProcessor(CommandProcessor):
    def process(self, message, screen):
        move = screen.printer.lookup_object("gcode_move")
        current_x = move.get_status()["gcode_position"].x
        if message.command_data[0] == 0x01:
            if move.absolute_coord:
                screen.run_delayed_gcode(F"G0 X{(current_x + screen._axis_unit)}")
            else:
                screen.run_delayed_gcode(F"G0 X+{screen._axis_unit}")
        else:
            if move.absolute_coord:
                screen.run_delayed_gcode(F"G0 X{(current_x - screen._axis_unit)}")
            else:
                screen.run_delayed_gcode(F"G0 X-{screen._axis_unit}")

class Heater0KeyProcessor(CommandProcessor): #heater temp
    def process(self, message, screen):
        temp = ((message.command_data[0] & 0xff00) >> 8) | ((message.command_data[0] & 0x00ff) << 8)
        screen.run_delayed_gcode(f"M104 S{temp}")
        screen.send_text("pretemp.nozzletemp.txt=\" {} / {}\"")

class HeaterBedKeyProcessor(CommandProcessor): #bed temp
    def process(self, message, screen):
        temp = ((message.command_data[0] & 0xff00) >> 8) | ((message.command_data[0] & 0x00ff) << 8)
        screen.run_delayed_gcode(f"M140 S{temp}")
        screen.send_text("pretemp.bedtemp.txt=\" {} / {}\"")           

class SettingScreenProcessor(CommandProcessor):
    def process(self, message, screen):
        if message.command_data[0] == 0x1:            
            screen.send_text("page autohome")
            screen.run_delayed_gcode("G28\ng1 f200 Z0.00", lambda: (
                screen.updateNumericVariable("leveling.va1.val", f"{screen._get_variant()}"),           
                screen.send_text("page leveldata_36"),
                screen.send_text("leveling_36.tm0.en=0"),
                screen.send_text("leveling.tm0.en=0")                
            ))   
        if message.command_data[0] == 0x6:
            screen.run_delayed_gcode("M84")
        if message.command_data[0] == 0x7:
            fan = screen.printer.lookup_object("fan")
            if fan.get_status(screen.reactor.monotonic())['speed']:
                screen.run_delayed_gcode(f"M106 S0")
                screen.updateNumericVariable("set.va0.val", "0")
            else:
                screen.run_delayed_gcode(f"M106 S255")
                screen.updateNumericVariable("set.va0.val", "1")
        if message.command_data[0] == 0xD:
            screen.send_text("page multiset")

class ResumePrintProcessor(CommandProcessor):
    def process(self, message, screen):
        if message.command_data[0] == 0x1:
            screen.updateNumericVariable("restFlag1", "0")
            screen.send_text("page wait")    
            screen.run_delayed_gcode("M24", lambda: (
                screen.send_text("page printpause")
            ))


class PausePrintProcessor(CommandProcessor):
    def process(self, message, screen):
        if message.command_data[0] == 0x1: #pause button pressed
            screen.send_text("page pauseconfirm")
        if message.command_data[0] == 0xF0: #cancel
            pass #do nothing, screen change handled in tft
        if message.command_data[0] == 0xF1: #pause button confirmed
            screen.updateNumericVariable("restFlag1", "1")
            screen.send_text("page wait")    
            screen.run_delayed_gcode("M25", lambda: (
                screen.send_text("page printpause")
            ))


class StopPrintProcessor(CommandProcessor):
    def process(self, message, screen):
        if message.command_data[0] == 0x1 or message.command_data[0] == 0xF1: #confirm stop print
            screen.send_text("page wait")    
            screen.run_delayed_gcode("CANCEL_PRINT", lambda: (
                screen.send_text("page main")
            ))
            
class HardwareTestProcessor(CommandProcessor):
    def process(self, message, screen):
        if message.command_data[0] == 0x0F: #HARDWARE TEST CALLED FROM MAIN SCREEN
            pass
            
class SettingBackProcessor(CommandProcessor):
    def restart_if_config_needed(self, screen):
        config = screen.printer.lookup_object("configfile").get_status(screen.reactor.monotonic())

        if config['save_config_pending']:
            screen.send_text("page wait")
            screen.run_delayed_gcode("SAVE_CONFIG")

    def process(self, message, screen): 
        if message.command_data[0] == 0x01: #setting back key from leveling
            screen.run_delayed_gcode("Z_OFFSET_APPLY_PROBE\nG0 Z.2", lambda:(
                self.restart_if_config_needed(screen)
            ))            

class PrintFileProcessor(CommandProcessor):
    def process(self, message, screen): 
        if message.command_data[0] == 0x01: #confirm print
            screen.run_delayed_gcode(f"M23 {screen._requested_file}\nM24")


class SelectFileProcessor(CommandProcessor):
    def process(self, message, screen):         
        screen.updateTextVariable("askprint.t0.txt", "")
        screen.updateTextVariable("printpause.t0.txt", "")

        max_file = len(screen._file_list) - 1
        requested_file = message.command_data[0] - 1

        if requested_file > max_file:
            screen.send_text("beep 2000")
        else:
            screen.updateTextVariable("askprint.t0.txt", screen._file_list[requested_file][0])
            screen.updateTextVariable("printpause.t0.txt", screen._file_list[requested_file][0])
            screen._requested_file = screen._file_list[requested_file][0]
            screen.send_text("page askprint")

class PowerContinueProcessor(CommandProcessor):
    def process(self, message, screen):  
        if message.command_data[0] == 0x03: #resume printing
            screen.send_text("page multiset")
        
CommandProcessors = [
    MainPageProcessor(DGUS_KEY_MAIN_PAGE),
    BedLevelProcessor(DGUS_KEY_BED_LEVEL),
    TempScreenProcessor(DGUS_KEY_TEMP_SCREEN),
    CoolScreenProcessor(DGUS_KEY_COOL_SCREEN),
    AxisPageSelectProcessor(DGUS_KEY_AXIS_PAGE_SELECT),
    ZAxisMoveKeyProcessor(DGUS_KEY_ZAXIS_MOVE_KEY),
    YAxisMoveKeyProcessor(DGUS_KEY_YAXIS_MOVE_KEY),
    XAxisMoveKeyProcessor(DGUS_KEY_XAXIS_MOVE_KEY),
    Heater0KeyProcessor(DGUS_KEY_HEATER0_TEMP_ENTER),
    HeaterBedKeyProcessor(DGUS_KEY_HOTBED_TEMP_ENTER),
    AdjustmentProcessor(DGUS_KEY_ADJUSTMENT),
    SettingScreenProcessor(DGUS_KEY_SETTING_SCREEN),
    ResumePrintProcessor(DGUS_KEY_RESUME_PRINT),
    PausePrintProcessor(DGUS_KEY_PAUSE_PRINT),
    StopPrintProcessor(DGUS_KEY_STOP_PRINT),
    HardwareTestProcessor(DGUS_KEY_HARDWARE_TEST),
    SettingBackProcessor(DGUS_KEY_SETTING_BACK_KEY),
    PrintFileProcessor(DGUS_KEY_PRINT_FILE),
    SelectFileProcessor(DGUS_KEY_SELECT_FILE),
    PowerContinueProcessor(DGUS_KEY_POWER_CONTINUE)
]
