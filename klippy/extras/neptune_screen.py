import logging
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
        self.log("Init")
        self._serial_state = None
        self._serial_state = SERIAL_STATE_HEADER_NONE
        self._axis_unit = 1
        self.printer = config.get_printer()
        self.name = config.get_name()
        self.reactor = self.printer.get_reactor()
        self.printer.load_object(config, 'heaters')
        self.log(config.getlist('heater', ("extruder", "heater_bed")))
        self.heater_names = config.getlist("heater", ("extruder", "heater_bed"))
        self._gcode_event_script = ''
        self.heaters = []
        self.leds = []

        self.printer.register_event_handler("klippy:ready", self.handle_ready)       
        self.gcode = self.printer.lookup_object('gcode')

        usart = config.get('usart')
       
        self.serial_bridge = self.printer.lookup_object(f'serial_bridge {usart}')
        self.serial_bridge.register_callback(self._handle_serial_bridge_response)

        self.log(f'{self.serial_bridge}')

        self._update_interval = 2
        self._update_timer = self.reactor.register_timer(self._screen_update)        

        self.input_buffer = ""

    def _screen_update(self, eventtime):
        self.log("Send update timer")
        for heater in self.heaters:            
            current_temp, target_temp = heater.get_temp(eventtime)            
            if(heater.name == 'heater_bed'):
                self.serial_bridge.send_text("main.bedtemp.txt=\"" f'{current_temp:.0f} / {target_temp:.0f}' + "\"")
            else:
                self.serial_bridge.send_text("main.nozzletemp.txt=\"" f'{current_temp:.0f} / {target_temp:.0f}' + "\"")
        
        if self._is_led_on(eventtime):
            self.serial_bridge.send_text("status_led2=1")
        else:
            self.serial_bridge.send_text("status_led2=0")

        g_status = self.printer.lookup_object("gcode_move").get_status()
        self.log(f"status: { g_status}")

        self.serial_bridge.send_text(f"main.xvalue.val={(g_status['position'].x * 100):.0f}")
        self.serial_bridge.send_text(f"main.yvalue.val={(g_status['position'].y * 100):.0f}")
        self.serial_bridge.send_text(f"main.zvalue.val={(g_status['position'].z * 1000):.0f}")

        self.serial_bridge.send_text(f"printpause.zvalue.val={(g_status['position'].z * 10):.0f}")
        
        heater_fans = self.printer.lookup_objects('heater_fan')
        #self.log(f'Fans: {fans}')

        for (name, fan) in heater_fans:
            pass
            #self.log(f'Fan status: {fan.get_status(eventtime)}')
            #self.serial_bridge.send_text(f"printpaause.fanspeed.txt={fan.get_status(eventtime)['speed'] * 100}")
        #fan = self.printer.lookup_object("heater_fan")
        #self.log(f"Heater fan: {fan.get_status()}")
        #

        fan = self.printer.lookup_object("fan")
        self.serial_bridge.send_text(f"printpause.fanspeed.txt=\"{(fan.get_status(eventtime)['speed'] * 100):.0f}%\"")

        #self.send

        return eventtime + self._update_interval

    def _is_led_on(self, eventtime):
        for led in self.leds:
            status = led.get_status(eventtime)
            self.log(f'{status}')
            white = status["color_data"][0][3]
            
            if(white > 0):
                return True
            else:
                return False

    def _handle_serial_bridge_response(self, data):
        messages = []
        message = Message()
        
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
                message.payload = []                
                message.length = byte
            elif self._serial_state == SERIAL_STATE_HEADER_MESSAGE:                
                message.payload.append(byte)

                if(len(message.payload) == message.length):
                    messages.append(message)
                    self._serial_state = SERIAL_STATE_HEADER_NONE

        self._serial_state = SERIAL_STATE_HEADER_NONE
        
        #self.log(f"Messages {str(messages)}")

        for message in messages:
            message.process_datagram()
            self.process_message(message)

        #self.input_buffer += params["text"].hex()
        #self._process_input_buffer()

    def process_message(self, message):
        self.log("Process message: " + str(message))

        move = self.printer.lookup_object("gcode_move")
        extrusion_factor = move.extrude_factor

        if(message.command == DGUS_CMD_READVAR):
            for proccessor in CommandProccessors:
                proccessor.process_if_match(message, self)

    def run_delayed_gcode(self, gcode):
        self._gcode_event_script = gcode
        self.reactor.register_timer(self.gcode_command_timer, self.reactor.monotonic())
        
    def gcode_command_timer(self, eventtime):
        self.gcode.run_script(self._gcode_event_script)
        self._gcode_event_script = ''
        return self.reactor.NEVER
        
    def _process_input_buffer(self):
        delimiterpos = self.input_buffer.rfind("\n")
        while delimiterpos > 0:
            commands = self.input_buffer[:delimiterpos]
            self.gcode.respond_info(commands, log=False)
            self.gcode._process_commands(commands.split("\n"))
            self.input_buffer = self.input_buffer[delimiterpos + 1:]
            delimiterpos = self.input_buffer.rfind("\n")

    def _screen_init(self, eventtime):
        self.log("Screen init")
        self.serial_bridge.send_text("page boot") 
        self.serial_bridge.send_text("com_star") 
        self.serial_bridge.send_text("main.va0.val=1") 
        self.serial_bridge.send_text("page main") 
        self.reactor.update_timer(self._update_timer, eventtime + self._update_interval)
        return self.reactor.NEVER

    def handle_ready(self):
        self.log("ready")
        
        pheaters = self.printer.lookup_object('heaters')
        self.heaters = [pheaters.lookup_heater(n) for n in self.heater_names]

        self.reactor.register_timer(self._screen_init, self.reactor.monotonic() + 2)        

        pled = self.printer.lookup_object("led")
        self.leds =  [pled.led_helpers.get(n) for n in pled.led_helpers.keys() ]


        for n in self.printer.lookup_objects():
            self.log(f"object: {n}" )
        
    def send_text(self, text):
        self.serial_bridge.send_text(text)
        
    def log(self, msg, *args, **kwargs):
        logging.info("Neptune Screen: " + str(msg))

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
DGUS_KEY_ADJUSTMENT = 0x1004
DGUS_KEY_TEMP_SCREEN = 0x1030
DGUS_KEY_COOL_SCREEN = 0x1032
DGUS_KEY_HEATER0_TEMP_ENTER = 0x1034
DGUS_KEY_HOTBED_TEMP_ENTER = 0x103A
DGUS_KEY_SETTING_SCREEN = 0x103E
DGUS_KEY_BED_LEVEL = 0x1044
DGUS_KEY_AXIS_PAGE_SELECT = 0x1046
DGUS_KEY_XAXIS_MOVE_KEY = 0x1048
DGUS_KEY_YAXIS_MOVE_KEY = 0x104A
DGUS_KEY_ZAXIS_MOVE_KEY = 0x104C

class CommandProccessor(ABC):
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
        
class MainPageProccessor(CommandProccessor):
    def process(self, message, screen):
        if message.command_data[0] == 0x1: #print button
            screen.send_text("page printpause")

class BedLevelProccessor(CommandProccessor):
    def process(self, message, screen):
        if message.command_data[0] == 0x8: #light button
            screen.log("Requested light toggle")
            pled = screen.printer.lookup_object("led")
            for n in pled.led_helpers.keys():
                status = pled.led_helpers[n].get_status(None)
                screen.log(f'{status}')
                white = status["color_data"][0][3]
                
                if(white > 0):
                    screen.gcode.run_script(f"SET_LED LED={n} WHITE=0")
                else:
                    screen.gcode.run_script(f"SET_LED LED={n} WHITE=1")

class TempScreenProccessor(CommandProccessor):
    def process(self, message, screen):
        if message.command_data[0] == 0x5: #.1mm
            screen._axis_unit = 0.1
        if message.command_data[0] == 0x6: #1mm
            screen._axis_unit = 1.0
        if message.command_data[0] == 0x7: #10mm
            screen._axis_unit = 10.0

class CoolScreenProccessor(CommandProccessor):
    def process(self, message, screen):
        if message.command_data[0] == 1: #power off hotend
            screen.run_delayed_gcode("M104 S0")
        if message.command_data[0] == 2: #power off bed
            screen.run_delayed_gcode("M140 S0")

class AxisPageSelectProccessor(CommandProccessor):
    def process(self, message, screen):
        if message.command_data[0] == 1:
            screen._axis_unit = 0.1
        elif message.command_data[0] == 2:
            screen._axis_unit = 1.0
        elif message.command_data[0] == 3:
            screen._axis_unit = 10
        elif message.command_data[0] == 4:
            screen.run_delayed_gcode("G28")
        elif message.command_data[0] == 5:
            screen.run_delayed_gcode("G28 X")
        elif message.command_data[0] == 6:
            screen.run_delayed_gcode("G28 Y")
        elif message.command_data[0] == 7:                  
            screen.run_delayed_gcode("G28 Z")

class ZAxisMoveKeyProccessor(CommandProccessor):
    def process(self, message, screen):
        move = screen.printer.lookup_object("gcode_move")
        current_z = move.get_status()["gcode_position"].z
        if message.command_data[0] == 0x01:
            if move.absolute_coord:
                screen.gcode.run_script(F"G0 Z{(current_z + screen._axis_unit)}")
            else:
                screen.gcode.run_script(F"G0 Z+{screen._axis_unit}")
        else:
            if move.absolute_coord:
                screen.gcode.run_script(F"G0 Z{(current_z - screen._axis_unit)}")
            else:
                screen.gcode.run_script(F"G0 Z-{screen._axis_unit}")

class YAxisMoveKeyProccessor(CommandProccessor):
    def process(self, message, screen):
        move = screen.printer.lookup_object("gcode_move")
        current_y = move.get_status()["gcode_position"].y
        if message.command_data[0] == 0x01:
            if move.absolute_coord:
                screen.gcode.run_script(F"G0 Y{(current_y + screen._axis_unit)}")
            else:
                screen.gcode.run_script(F"G0 Y+{screen._axis_unit}")
        else:
            if move.absolute_coord:
                screen.gcode.run_script(F"G0 Y{(current_y - screen._axis_unit)}")
            else:
                screen.gcode.run_script(F"G0 Y-{screen._axis_unit}")

class XAxisMoveKeyProccessor(CommandProccessor):
    def process(self, message, screen):
        move = screen.printer.lookup_object("gcode_move")
        current_x = move.get_status()["gcode_position"].x
        if message.command_data[0] == 0x01:
            if move.absolute_coord:
                screen.gcode.run_script(F"G0 X{(current_x + screen._axis_unit)}")
            else:
                screen.gcode.run_script(F"G0 X+{screen._axis_unit}")
        else:
            if move.absolute_coord:
                screen.gcode.run_script(F"G0 X{(current_x - screen._axis_unit)}")
            else:
                screen.gcode.run_script(F"G0 X-{screen._axis_unit}")

class Heater0KeyProccessor(CommandProccessor): #heater temp
    def process(self, message, screen):
        temp = ((message.command_data[0] & 0xff00) >> 8) | ((message.command_data[0] & 0x00ff) << 8)
        screen.run_delayed_gcode(f"M104 S{temp}")
        screen.send_text("pretemp.nozzletemp.txt=\" {} / {}\"")

class HeaterBedKeyProccessor(CommandProccessor): #bed temp
    def process(self, message, screen):
        temp = ((message.command_data[0] & 0xff00) >> 8) | ((message.command_data[0] & 0x00ff) << 8)
        screen.run_delayed_gcode(f"M140 S{temp}")
        screen.send_text("pretemp.bedtemp.txt=\" {} / {}\"")   

class AdjustmentProccessor(CommandProccessor):
    def process(self, message, screen):
        if message.command_data[0] == 0x02:
            screen.send_text("page printpause")

class SettingScreenProccessor(CommandProccessor):
    def process(self, message, screen):
        if message.command_data[0] == 0x1:
            screen.run_delayed_gcode("G28\ng1 f200 Z0.05")                    
            screen.send_text("page leveldata_36")
            screen.send_text("leveling_36.tm0.en=0")

CommandProccessors = [
    MainPageProccessor(DGUS_KEY_MAIN_PAGE),
    BedLevelProccessor(DGUS_KEY_BED_LEVEL),
    TempScreenProccessor(DGUS_KEY_TEMP_SCREEN),
    CoolScreenProccessor(DGUS_KEY_COOL_SCREEN),
    AxisPageSelectProccessor(DGUS_KEY_AXIS_PAGE_SELECT),
    ZAxisMoveKeyProccessor(DGUS_KEY_ZAXIS_MOVE_KEY),
    YAxisMoveKeyProccessor(DGUS_KEY_YAXIS_MOVE_KEY),
    XAxisMoveKeyProccessor(DGUS_KEY_XAXIS_MOVE_KEY),
    Heater0KeyProccessor(DGUS_KEY_HEATER0_TEMP_ENTER),
    HeaterBedKeyProccessor(DGUS_KEY_HOTBED_TEMP_ENTER),
    AdjustmentProccessor(DGUS_KEY_ADJUSTMENT),
    SettingScreenProccessor(DGUS_KEY_SETTING_SCREEN)
]