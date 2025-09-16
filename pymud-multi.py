import argparse
import copy
import json
import os
import pickle
import queue
import random
import re
import requests
import signal
import sys
import threading
import time
import traceback
import textwrap
import socket
from abc import ABC, abstractmethod

# Global debug flag
DEBUG = False

def debug_print(*args, **kwargs):
    """Print debug message only if DEBUG flag is enabled"""
    if DEBUG:
        print("DEBUG:", *args, **kwargs)

# Load configuration
def load_config():
    try:
        with open('config.json', 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"Warning: Could not load config.json ({e}), using defaults")
        return {
            "server": {"host": "0.0.0.0", "port": 4002},
            "llm": {
                "server_ip": "127.0.0.1", "server_port": 1337,
                "model": "gpt-3.5-turbo", "max_tokens": 150,
                "temperature": 0.8, "frequency_penalty": 0.5,
                "presence_penalty": 1.0, "top_p": 0.95
            }
        }

config = load_config()

# LLM Chat Function
def llm_chat(conversation_history):
    print(f"DEBUG CHAT: Starting LLM chat request...")
    print(f"DEBUG CHAT: Conversation history length: {len(conversation_history)}")
    
    llm_config = config['llm']
    llm_url = f"http://{llm_config['server_ip']}:{llm_config['server_port']}/v1/chat/completions"
    
    print(f"DEBUG CHAT: Using LLM server: {llm_url}")
    print(f"DEBUG CHAT: Model: {llm_config['model']}")
    print(f"DEBUG CHAT: Max tokens: {llm_config['max_tokens']}")
    
    data = {
        "model": llm_config['model'],
        "messages": conversation_history,
        "max_tokens": llm_config['max_tokens'],
        "temperature": llm_config['temperature'],
        "frequency_penalty": llm_config['frequency_penalty'],
        "presence_penalty": llm_config['presence_penalty'],
        "top_p": llm_config['top_p']
    }
    headers = {'Content-Type': 'application/json'}
    
    print(f"DEBUG CHAT: Request data: {data}")
    print(f"DEBUG CHAT: Request headers: {headers}")
    
    try:
        print(f"DEBUG CHAT: Sending POST request to {llm_url}")
        response = requests.post(llm_url, json=data, headers=headers, timeout=30)
        
        print(f"DEBUG CHAT: Response status code: {response.status_code}")
        print(f"DEBUG CHAT: Response headers: {dict(response.headers)}")
        
        if response.status_code != 200:
            print(f"DEBUG CHAT: Error response content: {response.text}")
        
        response.raise_for_status()
        result = response.json()
        
        print(f"DEBUG CHAT: Response JSON keys: {list(result.keys())}")
        print(f"DEBUG CHAT: Full response: {result}")
        
        ai_reply = result.get('choices', [{}])[0].get('message', {}).get('content', '').strip()
        
        print(f"DEBUG CHAT: Extracted AI reply: '{ai_reply}'")
        
        if not ai_reply:
            ai_reply = "I'm sorry, I don't have anything to say right now."
            print(f"DEBUG CHAT: Using fallback message")
        
        print(f"DEBUG CHAT: Returning successful response")
        return ai_reply
        
    except requests.exceptions.ConnectionError as e:
        print(f"DEBUG CHAT: Connection error - server likely not running: {e}")
        print(f"DEBUG CHAT: Make sure LLM server is running on {llm_url}")
        return "I'm sorry, the AI service is not available right now."
        
    except requests.exceptions.Timeout as e:
        print(f"DEBUG CHAT: Request timeout: {e}")
        return "I'm sorry, the AI service is taking too long to respond."
        
    except requests.exceptions.HTTPError as e:
        print(f"DEBUG CHAT: HTTP error: {e}")
        print(f"DEBUG CHAT: Response content: {response.text if 'response' in locals() else 'No response'}")
        return "I'm sorry, there was an error with the AI service."
        
    except (ValueError, KeyError) as e:
        print(f"DEBUG CHAT: JSON parsing or key error: {e}")
        print(f"DEBUG CHAT: Response content: {response.text if 'response' in locals() else 'No response'}")
        return "I'm sorry, the AI service returned an invalid response."
        
    except Exception as e:
        print(f"DEBUG CHAT: Unexpected error: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return "I'm sorry, there was an unexpected error with the AI service."

# Connection Handler Architecture
class ConnectionHandler(ABC):
    """Abstract base class for handling different connection types"""
    
    @abstractmethod
    def send_message(self, message):
        """Send a message to the player"""
        pass
    
    @abstractmethod
    def receive_line(self):
        """Receive a line of input from the player"""
        pass
    
    @abstractmethod
    def close_connection(self):
        """Close the player's connection"""
        pass
    
    @abstractmethod
    def is_connected(self):
        """Check if the connection is still active"""
        pass

class TelnetConnectionHandler(ConnectionHandler):
    """Handles telnet socket connections"""
    
    def __init__(self, client_socket):
        self.client_socket = client_socket
        # Set socket timeout to prevent hanging connections
        if client_socket:
            client_socket.settimeout(30.0)  # 30 second timeout for operations
    
    def send_message(self, message):
        """Send message to telnet client"""
        if self.client_socket and self.is_connected():
            try:
                self.client_socket.sendall(message.encode('utf-8'))
            except (ConnectionResetError, BrokenPipeError, OSError):
                print(f"Connection lost for telnet client")
    
    def receive_line(self):
        """Receive line from telnet client"""
        if not self.client_socket or not self.is_connected():
            return None  # Signal connection loss
        try:
            raw_data = self.client_socket.recv(1024)
            if not raw_data:  # Connection closed
                return None
            # Handle telnet control codes and invalid UTF-8 gracefully
            try:
                data = raw_data.decode('utf-8', errors='ignore').strip()
            except UnicodeDecodeError:
                # Skip invalid UTF-8 data (telnet control codes)
                return ""
            return data  # Can be empty string if user just pressed enter
        except socket.timeout:
            print(f"Socket timeout while receiving data from telnet client")
            return None  # Signal connection loss
        except (ConnectionResetError, BrokenPipeError, OSError):
            return None  # Signal connection loss
    
    def close_connection(self):
        """Close telnet connection"""
        if self.client_socket:
            try:
                self.client_socket.close()
            except:
                pass
            self.client_socket = None
    
    def is_connected(self):
        """Check if telnet connection is active"""
        return self.client_socket is not None

class WebConnectionHandler(ConnectionHandler):
    """Handles web socket connections"""
    
    def __init__(self, session_id, socketio_instance):
        self.session_id = session_id
        self.socketio = socketio_instance
        self.pending_input = queue.Queue()
        self.connected = True
    
    def send_message(self, message):
        """Send message to web client"""
        if self.is_connected():
            try:
                print(f"DEBUG WEB SEND: Sending to {self.session_id}: {message.strip()}")
                # Use threading to ensure Socket.IO emission doesn't block
                import threading
                def emit_message():
                    try:
                        self.socketio.emit('message', {'content': message}, room=self.session_id)
                        print(f"DEBUG WEB SEND: Successfully emitted to {self.session_id}")
                    except Exception as e:
                        print(f"DEBUG WEB SEND: Emission failed for {self.session_id}: {e}")
                        self.connected = False

                # Run emission in a separate thread to avoid blocking
                threading.Thread(target=emit_message, daemon=True).start()

            except Exception as e:
                print(f"Error sending to web client {self.session_id}: {e}")
                self.connected = False
        else:
            print(f"DEBUG WEB SEND: Cannot send, session {self.session_id} not connected")
    
    def receive_line(self):
        """Receive line from web client (blocking)"""
        try:
            # This would be used for synchronous input in web interface
            # For now, return None to indicate async nature
            return None
        except:
            return None
    
    def close_connection(self):
        """Close web connection"""
        self.connected = False
        try:
            self.socketio.disconnect(self.session_id)
        except:
            pass
    
    def is_connected(self):
        """Check if web connection is active"""
        return self.connected

# Global variables for game state
rooms = {}
mobiles = {}
objects = {}
resets = {}
spells = {}
all_npcs = []
achievements = {}
current_weather = 'clear'
weather_conditions = ['clear', 'rainy', 'foggy', 'stormy']
crafting_recipes = {}
current_time_of_day = 'day'  # 'day' or 'night'

players = {}  # Key: player name, Value: Player object
players_lock = threading.Lock()
active_events = {}  # room_vnum -> event data
active_events_lock = threading.Lock()
combatants = {}  # Combat pairs tracking
combatants_lock = threading.Lock()
chat_sessions = {}  # Key: room_vnum, Value: {'npc': NPC object, 'player': Player object, 'conversation': [...]}  
chat_sessions_lock = threading.Lock()
portal_connections = {}  # Key: room_vnum, Value: destination_room_vnum
merchant_items = [
    {"vnum": 9001, "keywords": ["healing", "potion"], "short_desc": "a healing potion",
     "long_desc": "A healing potion glows softly here.", "description": "This potion restores health.",
     "item_type": "potion", "effects": {"heal": 50}},
    {"vnum": 9002, "keywords": ["magic", "scroll"], "short_desc": "a scroll of power",
     "long_desc": "A magical scroll lies here.", "description": "This scroll increases magical power.",
     "item_type": "scroll", "effects": {"mana": 25}},
    {"vnum": 9003, "keywords": ["exotic", "ring"], "short_desc": "an exotic ring",
     "long_desc": "An exotic ring sparkles here.", "description": "This ring has mysterious properties.",
     "item_type": "ring", "effects": {"magic": 3}},
    {"vnum": 9004, "keywords": ["rare", "amulet"], "short_desc": "a rare amulet",
     "long_desc": "A rare amulet hangs here.", "description": "This amulet protects the wearer.",
     "item_type": "amulet", "effects": {"protection": 2}},
    # Weapons
    {"vnum": 6004, "keywords": ["sword", "steel", "blade"], "short_desc": "a steel sword",
     "long_desc": "A gleaming steel sword lies here.", "description": "A well-balanced steel sword with a sharp edge.",
     "item_type": "weapon", "effects": {"attack": 10}},
    {"vnum": 6005, "keywords": ["dagger", "knife", "blade"], "short_desc": "a sharp dagger",
     "long_desc": "A sharp dagger has been dropped here.", "description": "A small but deadly dagger, perfect for quick strikes.",
     "item_type": "weapon", "effects": {"attack": 6}},
    {"vnum": 6006, "keywords": ["staff", "magic", "wooden"], "short_desc": "a wooden staff",
     "long_desc": "A wooden staff rests against the wall.", "description": "A wooden staff imbued with magical energy.",
     "item_type": "weapon", "effects": {"attack": 8, "magic": 5}},
    # Armor
    {"vnum": 6007, "keywords": ["armor", "leather", "chest"], "short_desc": "leather armor",
     "long_desc": "A suit of leather armor lies here.", "description": "Sturdy leather armor that provides good protection.",
     "item_type": "armor", "effects": {"defense": 8}},
    {"vnum": 6008, "keywords": ["chain", "chainmail", "armor"], "short_desc": "chainmail armor",
     "long_desc": "A suit of chainmail armor gleams here.", "description": "Heavy chainmail armor offering excellent protection.",
     "item_type": "armor", "effects": {"defense": 15}},
    # Shield
    {"vnum": 6009, "keywords": ["shield", "iron", "round"], "short_desc": "an iron shield",
     "long_desc": "An iron shield rests here.", "description": "A sturdy iron shield that can deflect attacks.",
     "item_type": "shield", "effects": {"defense": 5}},
    # Ring
    {"vnum": 6010, "keywords": ["ring", "power", "golden"], "short_desc": "a golden ring of power",
     "long_desc": "A golden ring of power sparkles here.", "description": "A magical ring that enhances the wearer's abilities.",
     "item_type": "ring", "effects": {"attack": 3, "defense": 3}}
]

# Colors for text formatting (Players see plain text as Telnet usually doesn't support these easily)
class Colors:
    RESET = ''
    RED = ''
    GREEN = ''
    YELLOW = ''
    BLUE = ''
    MAGENTA = ''
    CYAN = ''
    BOLD = ''
    WHITE = ''

# Data classes for game entities
class Room:
    def __init__(self, vnum, name, description, exits):
        self.vnum = vnum
        self.name = name
        self.description = description
        self.exits = exits  # Dictionary of exits
        self.mobs = []
        self.objects = []
        self.npcs = []  # Add npcs list
        self.extra_descriptions = []

class Mobile:
    def __init__(self, vnum, keywords, short_desc, long_desc,
                 description, level, is_npc=False, personality='',
                 background='', secrets='', schedule=None, inventory=None,
                 special_ability=None, room_vnum=None, tameable=False):
        self.vnum = vnum
        self.keywords = keywords
        self.short_desc = short_desc
        self.long_desc = long_desc
        self.description = description
        self.level = level
        self.is_npc = is_npc
        self.personality = personality
        self.background = background
        self.secrets = secrets
        self.schedule = schedule if schedule else []
        self.inventory = inventory if inventory else []
        self.special_ability = special_ability
        try:
            self.current_room = rooms.get(room_vnum) if room_vnum else None
        except (NameError, AttributeError):
            self.current_room = None
        self.conversation_history = []
        self.has_given_items = False
        self.quest = None
        self.hp = self.level * 10
        self.max_hp = self.hp
        self.defense = self.level * 2
        self.attack_power = self.level * 2
        self.tameable = tameable
        self.status_effects = []

class Object:
    def __init__(self, vnum, keywords, short_desc, long_desc,
                 description, item_type, effects):
        self.vnum = vnum
        self.keywords = keywords
        self.short_desc = short_desc
        self.long_desc = long_desc
        self.description = description
        self.item_type = item_type
        self.effects = effects

class Spell:
    def __init__(self, name, description, effect_func, mana_cost):
        self.name = name
        self.description = description
        self.effect_func = effect_func
        self.mana_cost = mana_cost

class Achievement:
    def __init__(self, name, description, is_unlocked=False):
        self.name = name
        self.description = description
        self.is_unlocked = is_unlocked

class Objective:
    def __init__(self, description):
        self.description = description
        self.is_completed = False

    def update(self):
        pass

class KillObjective(Objective):
    def __init__(self, mob_name, required_kills):
        super().__init__(f"Defeat {required_kills} {mob_name}(s)")
        self.mob_name = mob_name
        self.required_kills = required_kills
        self.current_kills = 0

    def update(self):
        if self.current_kills >= self.required_kills:
            self.is_completed = True

class CollectObjective(Objective):
    def __init__(self, item_name, required_amount):
        super().__init__(f"Collect {required_amount} {item_name}(s)")
        self.item_name = item_name
        self.required_amount = required_amount
        self.current_amount = 0

    def update(self, player):
        count = sum(1 for item in player.inventory if self.item_name in item.keywords)
        self.current_amount = count
        if self.current_amount >= self.required_amount:
            self.is_completed = True

class Quest:
    def __init__(self, name, description, objectives, rewards):
        self.name = name
        self.description = description
        self.objectives = objectives
        self.rewards = rewards
        self.is_completed = False

    def check_completion(self):
        if all(obj.is_completed for obj in self.objectives):
            self.is_completed = True
            return True
        return False

# Spell effect functions
def fireball_effect(caster, target):
    if target:
        damage = caster.intelligence * 2 + random.randint(5, 15)
        target.hp -= damage
        send_to_player(caster, f"You hurl a fireball at {target.short_desc}, dealing {damage} damage!\n")
        if target.hp <= 0:
            send_to_player(caster, f"{target.short_desc} has been defeated by your spell!\n")
    else:
        send_to_player(caster, "There is no target to cast fireball on.\n")

def magic_missile_effect(caster, target):
    if target:
        damage = caster.intelligence + random.randint(3, 10)
        target.hp -= damage
        send_to_player(caster, f"Magic missiles strike {target.short_desc}, dealing {damage} damage!\n")
        if target.hp <= 0:
            send_to_player(caster, f"{target.short_desc} has been defeated by your spell!\n")
    else:
        send_to_player(caster, "There is no target to cast magic missile on.\n")

def heal_effect(caster, target=None):
    heal_amount = caster.intelligence * 2 + random.randint(5, 15)
    caster.hp = min(caster.max_hp, caster.hp + heal_amount)
    send_to_player(caster, f"You cast heal on yourself, restoring {heal_amount} HP.\n")

def parse_area_file(file_path):
    with open(file_path, 'r') as f:
        lines = f.readlines()

    idx = 0
    section = None
    while idx < len(lines):
        line = lines[idx].strip()
        if line.startswith('#MOBOLD'):
            section = 'MOBOLD'
            idx += 1
        elif line.startswith('#OBJOLD'):
            section = 'OBJOLD'
            idx += 1
        elif line.startswith('#ROOMS'):
            section = 'ROOMS'
            idx += 1
        elif line.startswith('#RESETS'):
            section = 'RESETS'
            idx += 1
        elif line.startswith('#SPECIALS'):
            section = 'SPECIALS'
            idx += 1
        elif line.startswith('#0') or line.strip() == 'S' or line.startswith('#$'):
            section = None
            idx += 1
        else:
            if section == 'MOBOLD':
                idx = parse_mob(lines, idx)
            elif section == 'OBJOLD':
                idx = parse_object(lines, idx)
            elif section == 'ROOMS':
                idx = parse_room(lines, idx)
            elif section == 'RESETS':
                idx = parse_reset(lines, idx)
            else:
                idx += 1

def parse_mob(lines, idx):
    if not lines[idx].startswith('#'):
        return idx + 1
    vnum = int(lines[idx][1:].strip())
    idx += 1
    keywords = lines[idx].strip('~').strip()
    idx += 1
    short_desc = lines[idx].strip('~').strip()
    idx += 1
    long_desc = lines[idx].strip('~').strip()
    idx += 1
    description = ''
    while not lines[idx].strip().endswith('~'):
        description += lines[idx] + ' '
        idx += 1
    description += lines[idx].strip('~').strip()
    idx += 1
    level = 1
    while idx < len(lines) and not lines[idx].startswith('#') and lines[idx].strip() != '':
        line = lines[idx].strip()
        if re.match(r'^\d+\s+\d+\s+\d+', line):
            level = int(line.split()[0])
            idx += 1
            break
        idx += 1
    mobiles[vnum] = Mobile(vnum, keywords, short_desc, long_desc,
                           description, level)
    return idx

def parse_object(lines, idx):
    # Objects are loaded from JSON, skip here
    return idx + 1

def parse_room(lines, idx):
    if not lines[idx].startswith('#'):
        return idx + 1
    vnum = int(lines[idx][1:].strip())
    idx += 1
    name = lines[idx].strip('~').strip()
    idx += 1
    description = ''
    while not lines[idx].strip().endswith('~'):
        description += lines[idx] + ' '
        idx += 1
    description += lines[idx].strip('~').strip()
    idx += 1
    room_flags_line = lines[idx].strip()
    room_flags_parts = room_flags_line.split()
    if len(room_flags_parts) >= 3:
        # room_flags = int(room_flags_parts[0])
        # sector_type = int(room_flags_parts[2])
        pass
    idx += 1
    exits = {}
    extra_descriptions = []
    while idx < len(lines) and lines[idx][0] in ('D', 'E'):
        line = lines[idx]
        if line.startswith('D'):
            direction = int(line[1])
            idx += 1
            exit_description = ''
            while not lines[idx].strip().endswith('~'):
                exit_description += lines[idx] + ' '
                idx += 1
            exit_description += lines[idx].strip('~').strip()
            idx += 1
            exit_keywords = ''
            while not lines[idx].strip().endswith('~'):
                exit_keywords += lines[idx] + ' '
                idx += 1
            exit_keywords += lines[idx].strip('~').strip()
            idx += 1
            door_data = lines[idx].strip().split()
            if len(door_data) >= 3:
                door_flags = int(door_data[0])
                key_vnum = int(door_data[1])
                to_room_vnum = int(door_data[2])
            else:
                door_flags = 0
                key_vnum = 0
                to_room_vnum = 0
            idx += 1
            exit_data = {
                'description': exit_description,
                'keywords': exit_keywords,
                'door_flags': door_flags,
                'key_vnum': key_vnum,
                'to_room_vnum': to_room_vnum,
                'is_open': door_flags in (0, 2),
                'is_locked': door_flags in (2, 3),
                'secret_code': None
            }
            if idx < len(lines) and lines[idx].startswith('SECRET_CODE'):
                secret_code_line = lines[idx].strip()
                secret_code_parts = secret_code_line.split(' ', 1)
                if len(secret_code_parts) == 2:
                    exit_data['secret_code'] = secret_code_parts[1]
                    exit_data['is_locked'] = True
                idx += 1
            exits[direction] = exit_data
        elif line.startswith('E'):
            idx += 1
            ed_keywords = ''
            while not lines[idx].strip().endswith('~'):
                ed_keywords += lines[idx] + ' '
                idx += 1
            ed_keywords += lines[idx].strip('~').strip()
            idx += 1
            ed_description = ''
            while not lines[idx].strip().endswith('~'):
                ed_description += lines[idx] + ' '
                idx += 1
            ed_description += lines[idx].strip('~').strip()
            idx += 1
            extra_descriptions.append({
                'keywords': ed_keywords,
                'description': ed_description
            })
        else:
            idx += 1
    while idx < len(lines) and not lines[idx].startswith('S'):
        idx += 1
    idx += 1
    room = Room(vnum, name, description, exits)
    room.extra_descriptions = extra_descriptions
    rooms[vnum] = room
    return idx

def parse_reset(lines, idx):
    while idx < len(lines) and not lines[idx].startswith('S'):
        line = lines[idx].strip()
        if line:
            resets[line] = line
        idx += 1
    return idx + 1

def process_resets():
    for room in rooms.values():
        room.objects = []
        room.mobs = []
    for reset in resets.values():
        parts = reset.split()
        if len(parts) < 4:
            continue
        command = parts[0]
        if command == 'M':
            _, _, mob_vnum, _, room_vnum = parts[:5]
            mob_vnum = int(mob_vnum)
            room_vnum = int(room_vnum)
            if room_vnum in rooms and mob_vnum in mobiles:
                mob_template = mobiles[mob_vnum]
                mob = copy.deepcopy(mob_template)
                rooms[room_vnum].mobs.append(mob)
        elif command == 'O':
            _, _, obj_vnum, _, room_vnum = parts[:5]
            obj_vnum = int(obj_vnum)
            room_vnum = int(room_vnum)
            if room_vnum in rooms and obj_vnum in objects:
                obj_template = objects[obj_vnum]
                obj = copy.deepcopy(obj_template)
                rooms[room_vnum].objects.append(obj)
        elif command == 'G':
            continue
    # Example placements
    goblin_rooms = [2203, 2204]
    for room_vnum in goblin_rooms:
        if room_vnum in rooms and 2300 in mobiles:
            goblin_template = mobiles[2300]
            goblin = copy.deepcopy(goblin_template)
            rooms[room_vnum].mobs.append(goblin)
    herb_rooms = [2205, 2206]
    for room_vnum in herb_rooms:
        if room_vnum in rooms and 6000 in objects:
            herb_template = objects[6000]
            herb = copy.deepcopy(herb_template)
            rooms[room_vnum].objects.append(herb)

def load_objects_from_file(file_path):
    with open(file_path, 'r') as f:
        object_data_list = json.load(f)
    for obj_data in object_data_list:
        keywords = obj_data['keywords']
        if isinstance(keywords, str):
            keywords = keywords.split()
        obj = Object(
            vnum=obj_data['vnum'],
            keywords=keywords,
            short_desc=obj_data['short_desc'],
            long_desc=obj_data['long_desc'],
            description=obj_data['description'],
            item_type=obj_data.get('item_type', 'misc'),
            effects=obj_data.get('effects', {})
        )
        objects[obj.vnum] = obj

def place_random_treasures():
    treasure_items = [copy.deepcopy(obj) for obj in objects.values() if obj.vnum >= 5000]
    room_list = list(rooms.values())
    for treasure in treasure_items:
        room = random.choice(room_list)
        room.objects.append(treasure)

def load_spells_from_file(file_path):
    with open(file_path, 'r') as f:
        spell_data_list = json.load(f)
    for spell_data in spell_data_list:
        # Create a spell object with all the JSON properties
        class SpellObject:
            def __init__(self, data):
                self.name = data['name']
                self.description = data['description']
                self.mana_cost = data['mana_cost']
                self.spell_type = data['spell_type']
                self.requires_target = data['requires_target']
                self.damage_multiplier = data.get('damage_multiplier', 1)
                self.base_damage = data.get('base_damage', [1, 6])
                # For healing spells
                self.heal_multiplier = data.get('heal_multiplier', 1)
                self.base_heal = data.get('base_heal', [5, 15])
        
        spell = SpellObject(spell_data)
        spells[spell.name.lower()] = spell
    
    print(f"Loaded {len(spells)} spells from {file_path}")
    for spell_name in spells:
        print(f"  - {spells[spell_name].name}")

def load_npcs_from_file(file_path):
    with open(file_path, 'r') as f:
        npc_data_list = json.load(f)
    for npc_data in npc_data_list:
        inventory = []
        for item_data in npc_data.get('inventory', []):
            obj_keywords = item_data['keywords']
            if isinstance(obj_keywords, str):
                obj_keywords = obj_keywords.split()
            obj = Object(
                vnum=item_data['vnum'],
                keywords=obj_keywords,
                short_desc=item_data['short_desc'],
                long_desc=item_data['long_desc'],
                description=item_data['description'],
                item_type=item_data.get('item_type', 'misc'),
                effects=item_data.get('effects', {})
            )
            inventory.append(obj)
        schedule = []
        for entry in npc_data.get('schedule', []):
            if isinstance(entry, list) and len(entry) == 2:
                schedule.append(tuple(entry))

        npc = Mobile(
            vnum=npc_data['vnum'],
            keywords=npc_data['keywords'],
            short_desc=npc_data['short_desc'],
            long_desc=npc_data['long_desc'],
            description=npc_data['description'],
            level=npc_data['level'],
            is_npc=npc_data['is_npc'],
            personality=npc_data.get('personality', ''),
            background=npc_data.get('background', ''),
            secrets=npc_data.get('secrets', ''),
            schedule=schedule,
            inventory=inventory,
            special_ability=None,
            room_vnum=npc_data.get('room_vnum'),
            tameable=npc_data.get('tameable', False)
        )
        all_npcs.append(npc)
        if 'quest' in npc_data:
            quest_data = npc_data['quest']
            objectives = []
            for obj_data in quest_data['objectives']:
                if obj_data['type'] == 'kill':
                    obj_ = KillObjective(obj_data['mob_name'], obj_data['required_kills'])
                elif obj_data['type'] == 'collect':
                    obj_ = CollectObjective(obj_data['item_name'], obj_data['required_amount'])
                else:
                    continue
                objectives.append(obj_)
            rewards = {}
            quest_rewards = quest_data.get('rewards', {})
            if 'experience' in quest_rewards:
                rewards['experience'] = quest_rewards['experience']
            if 'items' in quest_rewards:
                rewards['items'] = []
                for item_data in quest_rewards['items']:
                    obj_keywords = item_data['keywords']
                    if isinstance(obj_keywords, str):
                        obj_keywords = obj_keywords.split()
                    it = {
                        'vnum': item_data['vnum'],
                        'keywords': obj_keywords,
                        'short_desc': item_data['short_desc'],
                        'long_desc': item_data['long_desc'],
                        'description': item_data['description'],
                        'item_type': item_data.get('item_type', 'misc'),
                        'effects': item_data.get('effects', {})
                    }
                    rewards['items'].append(it)
            quest = Quest(
                name=quest_data['name'],
                description=quest_data['description'],
                objectives=objectives,
                rewards=rewards
            )
            npc.quest = quest

        room_vnum = npc_data['room_vnum']
        if room_vnum in rooms:
            rooms[room_vnum].mobs.append(npc)
            npc.current_room = rooms[room_vnum]

direction_map = {
    0: 'north',
    1: 'east',
    2: 'south',
    3: 'west',
    4: 'up',
    5: 'down'
}

reverse_direction_map = {v: k for k, v in direction_map.items()}

command_abbreviations = {
    'n': 'north',
    's': 'south',
    'e': 'east',
    'w': 'west',
    'u': 'up',
    'd': 'down',
    'a': 'attack',
    'sp': 'special',
    'spec': 'special',
    'fb': 'fireball',
    'mm': 'magic missile',
    'h': 'heal',
    'o': 'open',
    'c': 'close',
    'l': 'look'
}

def is_valid_text(text):
    return bool(text.strip())

class Player:
    def __init__(self, name, current_room_vnum, connection_handler):
        self.name = name
        self.current_room = rooms[current_room_vnum]
        self.connection_handler = connection_handler
        # Keep client_socket for backward compatibility
        self.client_socket = getattr(connection_handler, 'client_socket', None)
        self.strength = 5
        self.agility = 5
        self.intelligence = 5
        self.vitality = 5
        self.skill_points = 0
        self.max_hp = self.calculate_max_hp()
        self.hp = self.max_hp
        self.max_mana = self.calculate_max_mana()
        self.mana = self.max_mana
        self.attack_power = self.calculate_attack_power()
        self.defense = self.calculate_defense()
        self.level = 1
        self.experience = 0
        self.inventory = []
        self.equipment = {
            'weapon': None,
            'armor': None,
            'shield': None,
            'ring': None,
            'amulet': None
        }
        self.resting = False
        self.rest_thread = None
        self.status_effects = []
        self.spellbook = {}
        self.gold = 100
        self.achievements = set()
        self.active_quests = []
        self.completed_quests = set()
        self.companion = None
        self.quests = []
        self.reputation = 0
        self.karma = 0
        self.achievements = []
        self.pets = []
        self.current_pet = None
        self.rooms_visited = set()

    def calculate_attack_power(self):
        return self.strength * 20

    def calculate_defense(self):
        return int(self.agility * 1.5)

    def calculate_max_hp(self):
        return self.vitality * 10

    def calculate_max_mana(self):
        return self.intelligence * 15

    def describe_current_room(self):
        # Safety check: ensure inventory is always a list
        if not isinstance(self.inventory, list):
            self.inventory = list(self.inventory) if hasattr(self.inventory, '__iter__') else []

        send_to_player(self, f"\n{self.current_room.name}\n")
        send_to_player(self, f"Weather: {current_weather.capitalize()}\n")
        send_to_player(self, f"Time: {current_time_of_day.capitalize()}\n")
        if current_time_of_day == 'night':
            send_to_player(self, "It's dark. You might need a light source.\n")
        send_to_player(self, f"{self.current_room.description}\n")
        exits = []
        for dir_num, exit_data in self.current_room.exits.items():
            direction = direction_map[dir_num]
            if exit_data['door_flags'] in (1, 3):
                if exit_data.get('is_open', False):
                    exits.append(direction)
                else:
                    exits.append(f"{direction} (closed door)")
            else:
                exits.append(direction)
        if exits:
            send_to_player(self, f"Exits: {', '.join(exits)}\n")
        else:
            send_to_player(self, "No obvious exits.\n")
        # Mobs
        for mob in self.current_room.mobs:
            send_to_player(self, f"You see {mob.short_desc} here.\n")
        # Objects
        for obj in self.current_room.objects:
            send_to_player(self, f"You see {obj.short_desc} here.\n")
        # Companion
        if self.companion:
            send_to_player(self, f"Your companion {self.companion.name} is here.\n")
        # Pet
        if self.current_pet:
            send_to_player(self, f"Your pet {self.current_pet.name} is here.\n")
        
        # Active events (like traveling merchants)
        if self.current_room.vnum in active_events:
            event = active_events[self.current_room.vnum]
            if event['type'] == 'merchant':
                merchant_name = event['data']['name']
                send_to_player(self, f"🚚 {merchant_name} has set up shop here with exotic wares! 🚚\n")

    def pick_up(self, obj):
        self.inventory.append(obj)
        send_to_player(self, f"You picked up {obj.short_desc}.\n")
        # Check achievements, update quests, etc.

    def show_inventory(self):
        send_to_player(self, "Inventory:\n")
        if self.inventory:
            for item in self.inventory:
                # Handle both dict and object items
                if hasattr(item, 'short_desc'):
                    item_name = item.short_desc
                elif isinstance(item, dict) and 'short_desc' in item:
                    item_name = item['short_desc']
                else:
                    item_name = str(item)
                send_to_player(self, f"- {item_name}\n")
        else:
            send_to_player(self, "Your inventory is empty.\n")

    def allocate_skill_points(self, skill_name, points):
        if points > self.skill_points:
            send_to_player(self, "You don't have enough skill points.\n")
            return
        if skill_name == 'strength':
            self.strength += points
        elif skill_name == 'agility':
            self.agility += points
        elif skill_name == 'intelligence':
            self.intelligence += points
        elif skill_name == 'vitality':
            self.vitality += points
        else:
            send_to_player(self, "Invalid skill name.\n")
            return
        self.skill_points -= points
        self.max_hp = self.calculate_max_hp()
        self.hp = self.max_hp
        self.max_mana = self.calculate_max_mana()
        self.mana = self.max_mana
        self.attack_power = self.calculate_attack_power()
        self.defense = self.calculate_defense()
        send_to_player(self, f"You have increased your {skill_name} by {points} points.\n")
        send_to_player(self, f"Remaining skill points: {self.skill_points}\n")

    def show_skills(self):
        send_to_player(self, "Your Skills:\n")
        send_to_player(self, f"Strength: {self.strength}\n")
        send_to_player(self, f"Agility: {self.agility}\n")
        send_to_player(self, f"Intelligence: {self.intelligence}\n")
        send_to_player(self, f"Vitality: {self.vitality}\n")
        send_to_player(self, f"Available Skill Points: {self.skill_points}\n")

    def move(self, direction):
        if self.resting:
            send_to_player(self, "You need to stand up before you can move.\n")
            return
        dir_num = reverse_direction_map.get(direction)
        if dir_num is not None and dir_num in self.current_room.exits:
            exit_data = self.current_room.exits[dir_num]
            if exit_data['door_flags'] in (1, 3):
                if not exit_data.get('is_open', False):
                    send_to_player(self, "The door is closed.\n")
                    return
                if exit_data.get('is_locked', False):
                    send_to_player(self, "The door is locked.\n")
                    return
            next_room_vnum = exit_data['to_room_vnum']
            if next_room_vnum in rooms:
                self.current_room = rooms[next_room_vnum]
                send_to_player(self, f"\nYou move {direction} to {self.current_room.name}.\n")
                self.describe_current_room()
                if self.companion:
                    self.companion.current_room = self.current_room
                if self.current_pet:
                    self.current_pet.current_room = self.current_room
                self.rooms_visited.add(self.current_room.vnum)
                # Small chance for lucky find while exploring
                trigger_lucky_find(self)
            else:
                send_to_player(self, "You can't go that way.\n")
        else:
            send_to_player(self, "You can't go that way.\n")

    def show_stats(self, brief=False):
        send_to_player(self, "\nPlayer Stats:\n")
        send_to_player(self, f"HP: {self.hp}/{self.max_hp}\n")
        send_to_player(self, f"Mana: {self.mana}/{self.max_mana}\n")
        send_to_player(self, f"Level: {self.level}\n")
        send_to_player(self, f"Experience: {self.experience}\n")
        send_to_player(self, f"Attack Power: {self.attack_power}\n")
        send_to_player(self, f"Defense: {self.defense}\n")
        send_to_player(self, f"Karma: {self.karma}\n")
        if self.status_effects:
            send_to_player(self, f"Status Effects: {[effect.name for effect in self.status_effects]}\n")
        else:
            send_to_player(self, "Status Effects: None\n")
        if self.companion:
            send_to_player(self, f"Companion: {self.companion.name}\n")
        if self.current_pet:
            send_to_player(self, f"Pet: {self.current_pet.name}\n")
        send_to_player(self, "Equipped Items:\n")
        for slot, item in self.equipment.items():
            if item:
                item_name = item.get('short_desc', 'unknown item') if isinstance(item, dict) else getattr(item, 'short_desc', 'unknown item')
                send_to_player(self, f"  {slot.capitalize()}: {item_name}\n")
            else:
                send_to_player(self, f"  {slot.capitalize()}: None\n")

    def rest(self):
        if self.hp >= self.max_hp and self.mana >= self.max_mana:
            send_to_player(self, "You are already at full health and mana.\n")
            return
        if self.resting:
            send_to_player(self, "You are already resting.\n")
            return
        send_to_player(self, "You sit down and begin to rest.\n")
        self.resting = True
        self.rest_thread = threading.Thread(target=self.heal_over_time)
        self.rest_thread.start()

    def stand(self):
        if not self.resting:
            send_to_player(self, "You are not resting.\n")
            return
        self.resting = False
        if self.rest_thread:
            try:
                self.rest_thread.join(timeout=5.0)
                if self.rest_thread.is_alive():
                    print(f"WARNING: Rest thread for {self.name} did not terminate within timeout")
            except Exception as e:
                print(f"ERROR: Exception while joining rest thread for {self.name}: {e}")
        send_to_player(self, "You stand up, feeling refreshed.\n")

    def heal_over_time(self):
        while self.resting and (self.hp < self.max_hp or self.mana < self.max_mana):
            time.sleep(1)
            self.hp = min(self.max_hp, self.hp + 5)
            self.mana = min(self.max_mana, self.mana + 5)
            send_to_player(self, f"You rest and recover 5 HP and 5 Mana. Current HP: {self.hp}/{self.max_hp}, Mana: {self.mana}/{self.max_mana}\n")
            if self.hp == self.max_hp and self.mana == self.max_mana:
                send_to_player(self, "You are fully healed and your mana is restored.\n")
                self.resting = False
                break

    def teleport(self, room_identifier):
        if self.resting:
            send_to_player(self, "You need to stand up before you can teleport.\n")
            return
        if room_identifier.isdigit():
            room_vnum = int(room_identifier)
            if room_vnum in rooms:
                self.current_room = rooms[room_vnum]
                send_to_player(self, f"You teleport to {self.current_room.name}.\n")
                self.describe_current_room()
                if self.companion:
                    self.companion.current_room = self.current_room
                if self.current_pet:
                    self.current_pet.current_room = self.current_room
                self.rooms_visited.add(self.current_room.vnum)
                return
            else:
                send_to_player(self, "No room with that number exists.\n")
                return
        for room in rooms.values():
            if room_identifier.lower() in room.name.lower():
                self.current_room = room
                send_to_player(self, f"You teleport to {self.current_room.name}.\n")
                self.describe_current_room()
                if self.companion:
                    self.companion.current_room = self.current_room
                if self.current_pet:
                    self.current_pet.current_room = self.current_room
                self.rooms_visited.add(self.current_room.vnum)
                return
        send_to_player(self, "No room with that name exists.\n")

    def show_map(self):
        send_to_player(self, "Map:\n")
        for dir_num, exit_data in self.current_room.exits.items():
            direction = direction_map[dir_num]
            to_room_vnum = exit_data['to_room_vnum']
            adjacent_room = rooms.get(to_room_vnum)
            if adjacent_room:
                send_to_player(self, f"{direction.capitalize()}: {adjacent_room.name}\n")
            else:
                send_to_player(self, f"{direction.capitalize()}: Unknown area\n")

    def cast_spell(self, spell_name, target=None):
        spell = self.spellbook.get(spell_name.lower())
        if not spell:
            send_to_player(self, "You don't know that spell.\n")
            return
        if self.mana < spell.mana_cost:
            send_to_player(self, "You don't have enough mana.\n")
            return
        self.mana -= spell.mana_cost
        send_to_player(self, f"You cast {spell.name}!\n")
        spell.effect_func(self, target)

    def view_achievements(self):
        send_to_player(self, "Achievements:\n")
        if self.achievements:
            for a in self.achievements:
                send_to_player(self, f"- {a.name}: {a.description}\n")
        else:
            send_to_player(self, "You have not unlocked any achievements yet.\n")

    def tame_mob(self, mob):
        if self.current_pet:
            send_to_player(self, "You already have an active pet.\n")
            return
        if mob and mob.tameable:
            if mob.hp > mob.max_hp * 0.5:
                send_to_player(self, "The creature is too strong to be tamed right now.\n")
                return
            success_chance = 0.3 + (self.level * 0.02)
            success_chance = min(success_chance, 0.8)
            if random.random() <= success_chance:
                pet = Pet(mob.short_desc, self.current_room)
                self.pets.append(pet)
                self.current_pet = pet
                self.current_room.mobs.remove(mob)
                send_to_player(self, f"You have successfully tamed {mob.short_desc} as your pet!\n")
                unlock_achievement('Pet Tamer', self)
            else:
                send_to_player(self, "Your taming attempt failed!\n")
        else:
            send_to_player(self, "You can't tame that creature.\n")

    def view_pets(self):
        send_to_player(self, "Your Pets:\n")
        if self.pets:
            for pet in self.pets:
                send_to_player(self, f"- {pet.name} (Level {pet.level})\n")
        else:
            send_to_player(self, "You don't have any pets.\n")

    def dismiss_pet(self):
        if self.current_pet:
            send_to_player(self, f"You dismiss your pet {self.current_pet.name}.\n")
            self.current_pet = None
        else:
            send_to_player(self, "You don't have an active pet to dismiss.\n")

    def craft_item(self, item1_name, item2_name):
        item1 = next((item for item in self.inventory if item1_name in item.keywords), None)
        item2 = next((item for item in self.inventory if item2_name in item.keywords), None)
        if item1 and item2:
            recipe = crafting_recipes.get((item1_name, item2_name))
            if recipe:
                self.inventory.remove(item1)
                self.inventory.remove(item2)
                new_item_template = objects.get(recipe['vnum'])
                if new_item_template:
                    new_item = copy.deepcopy(new_item_template)
                    self.inventory.append(new_item)
                    send_to_player(self, f"You crafted {new_item.short_desc}!\n")
                    unlock_achievement('Master Crafter', self)
                else:
                    send_to_player(self, "Crafting failed: Resulting item not found.\n")
            else:
                send_to_player(self, "You cannot craft these items together.\n")
        else:
            send_to_player(self, "You don't have the required items to craft.\n")

class Pet:
    def __init__(self, name, current_room):
        self.name = name
        self.current_room = current_room
        self.max_hp = 30
        self.hp = self.max_hp
        self.attack_power = 10
        self.defense = 3
        self.level = 1
        self.experience = 0

    def attack(self, mob):
        damage = random.randint(3, self.attack_power) - mob.defense
        damage = max(1, damage)
        mob.hp -= damage

class Companion:
    def __init__(self, name, current_room):
        self.name = name
        self.current_room = current_room
        self.max_hp = 50
        self.hp = self.max_hp
        self.attack_power = 15
        self.defense = 5
        self.level = 1
        self.experience = 0

    def attack(self, mob):
        damage = random.randint(5, self.attack_power) - mob.defense
        damage = max(1, damage)
        mob.hp -= damage

class HealerCompanion(Companion):
    def __init__(self, name, current_room):
        super().__init__(name, current_room)
        self.healing_power = 10

    def heal_player(self, player):
        heal_amount = self.healing_power + self.level * 2
        player.hp = min(player.max_hp, player.hp + heal_amount)
        send_to_player(player, f"{self.name} heals you for {heal_amount} HP.\n")

class WarriorCompanion(Companion):
    def __init__(self, name, current_room):
        super().__init__(name, current_room)
        self.max_hp = 80
        self.hp = self.max_hp
        self.attack_power = 25
        self.defense = 10

def save_player_profile(player):
    """Save player profile to disk"""
    try:
        # Create player_saves directory if it doesn't exist
        base_dir = os.path.dirname(os.path.abspath(__file__))
        player_saves_dir = os.path.join(base_dir, 'player_saves')
        if not os.path.exists(player_saves_dir):
            os.makedirs(player_saves_dir)
        
        # Create profile data
        profile_data = {
            'name': player.name,
            'level': player.level,
            'experience': player.experience,
            'hit_points': player.hp,
            'max_hit_points': player.max_hp,
            'mana': player.mana,
            'max_mana': player.max_mana,
            'strength': player.strength,
            'agility': player.agility,
            'intelligence': player.intelligence,
            'vitality': player.vitality,
            'current_room_vnum': player.current_room.vnum if player.current_room else 2201,
            'inventory': [],
            'equipment': {},
            'spellbook': {},
            'gold': player.gold,
            'achievements': list(player.achievements),
            'active_quests': [quest.__dict__ for quest in player.active_quests],
            'completed_quests': list(player.completed_quests)
        }
        
        # Convert inventory items to saveable format
        for item in player.inventory:
            if hasattr(item, '__dict__'):
                # Object instance
                profile_data['inventory'].append(item.__dict__)
            else:
                # Dictionary item
                profile_data['inventory'].append(item)
        
        # Convert equipment to saveable format
        for slot, item in player.equipment.items():
            if item:
                if hasattr(item, '__dict__'):
                    profile_data['equipment'][slot] = item.__dict__
                else:
                    profile_data['equipment'][slot] = item
        
        # Convert spellbook to saveable format (save only spell names)
        for spell_name, spell in player.spellbook.items():
            # Just save the spell name, we'll reload from spells.json
            profile_data['spellbook'][spell_name] = {
                'name': spell.name,
                'learned': True
            }
        
        # Save to file using absolute path
        base_dir = os.path.dirname(os.path.abspath(__file__))
        filename = os.path.join(base_dir, 'player_saves', f'{player.name.lower()}.json')
        with open(filename, 'w') as f:
            json.dump(profile_data, f, indent=2)
        
        print(f"Saved profile for {player.name}")
        
    except Exception as e:
        print(f"Error saving player profile for {player.name}: {e}")
        traceback.print_exc()

def load_player_profile(player):
    """Load player profile from disk"""
    try:
        # Use absolute path to be safe
        base_dir = os.path.dirname(os.path.abspath(__file__))
        filename = os.path.join(base_dir, 'player_saves', f'{player.name.lower()}.json')
        
        if not os.path.exists(filename):
            print(f"No saved profile found for {player.name}, using defaults")
            return
        
        with open(filename, 'r') as f:
            profile_data = json.load(f)
        
        # Load basic stats
        player.level = profile_data.get('level', 1)
        player.experience = profile_data.get('experience', 0)
        player.hp = profile_data.get('hit_points', player.max_hp)
        player.max_hp = profile_data.get('max_hit_points', player.max_hp)
        player.mana = profile_data.get('mana', player.max_mana)
        player.max_mana = profile_data.get('max_mana', player.max_mana)

        # CRITICAL FIX: Ensure player never starts with 0 or negative HP
        if player.hp <= 0:
            print(f"WARNING: Player {player.name} had {player.hp} HP, setting to full health")
            player.hp = player.max_hp

        # Ensure HP doesn't exceed max HP
        if player.hp > player.max_hp:
            player.hp = player.max_hp
        player.strength = profile_data.get('strength', 5)
        player.agility = profile_data.get('agility', 5)
        player.intelligence = profile_data.get('intelligence', 5)
        player.vitality = profile_data.get('vitality', 5)
        # Add gold attribute if it doesn't exist
        if not hasattr(player, 'gold'):
            player.gold = 100
        player.gold = profile_data.get('gold', 100)
        
        # Load achievements (handle potential nested structures)
        achievements_data = profile_data.get('achievements', [])
        if achievements_data:
            try:
                player.achievements = set(achievements_data)
            except TypeError:
                # Handle unhashable types in achievements
                player.achievements = set()
                for achievement in achievements_data:
                    if isinstance(achievement, (str, int)):
                        player.achievements.add(achievement)
        else:
            player.achievements = set()
        
        # Load completed quests
        player.completed_quests = set(profile_data.get('completed_quests', []))
        
        # Load inventory
        player.inventory = []
        for item_data in profile_data.get('inventory', []):
            # Create Object instance from saved data with required parameters
            obj = Object(
                vnum=item_data.get('vnum', 0),
                keywords=item_data.get('keywords', []),
                short_desc=item_data.get('short_desc', 'an item'),
                long_desc=item_data.get('long_desc', 'Nothing special about it.'),
                description=item_data.get('description', 'A generic item.'),
                item_type=item_data.get('item_type', 'misc'),
                effects=item_data.get('effects', {})
            )
            # Set any additional attributes
            for key, value in item_data.items():
                if hasattr(obj, key):
                    setattr(obj, key, value)
            player.inventory.append(obj)
        
        # Load equipment
        player.equipment = {
            'weapon': None,
            'armor': None,
            'shield': None,
            'helmet': None,
            'boots': None,
            'gloves': None,
            'ring': None,
            'necklace': None
        }
        for slot, item_data in profile_data.get('equipment', {}).items():
            if item_data:
                obj = Object(
                    vnum=item_data.get('vnum', 0),
                    keywords=item_data.get('keywords', []),
                    short_desc=item_data.get('short_desc', 'an item'),
                    long_desc=item_data.get('long_desc', 'Nothing special about it.'),
                    description=item_data.get('description', 'A generic item.'),
                    item_type=item_data.get('item_type', 'misc'),
                    effects=item_data.get('effects', {})
                )
                # Set any additional attributes
                for key, value in item_data.items():
                    if hasattr(obj, key):
                        setattr(obj, key, value)
                player.equipment[slot] = obj
        
        # Load spellbook
        player.spellbook = {}
        for spell_name, spell_data in profile_data.get('spellbook', {}).items():
            if spell_name in spells:
                player.spellbook[spell_name] = spells[spell_name]

        # Load current room location
        saved_room_vnum = profile_data.get('current_room_vnum', 2201)
        if saved_room_vnum in rooms:
            # Remove player from current room if they're in one
            if player.current_room and hasattr(player.current_room, 'players'):
                if player in player.current_room.players:
                    player.current_room.players.remove(player)

            # Move player to saved room
            player.current_room = rooms[saved_room_vnum]

            # Add player to new room's player list
            if not hasattr(player.current_room, 'players'):
                player.current_room.players = []
            if player not in player.current_room.players:
                player.current_room.players.append(player)

            print(f"Player {player.name} restored to room {saved_room_vnum} ({player.current_room.name})")
        else:
            print(f"Warning: Saved room {saved_room_vnum} not found, keeping player in default room")

        # Load active quests (simplified for now)
        player.active_quests = []

        print(f"Loaded profile for {player.name}")
        
    except Exception as e:
        print(f"Error loading player profile for {player.name}: {e}")
        traceback.print_exc()

def npc_movement_loop():
    while True:
        current_time = time.localtime().tm_hour
        for npc in all_npcs:
            for schedule_entry in npc.schedule:
                try:
                    schedule_time, room_vnum = schedule_entry
                    if schedule_time == current_time and npc.current_room and npc.current_room.vnum != room_vnum:
                        if room_vnum in rooms:
                            if npc.current_room and npc in npc.current_room.mobs:
                                npc.current_room.mobs.remove(npc)
                            npc.current_room = rooms[room_vnum]
                            npc.current_room.mobs.append(npc)
                except ValueError:
                    pass
        time.sleep(60)

def spawn_merchant_event(room_vnum):
    """Spawn a traveling merchant event in a specific room"""
    debug_print(f"Spawning merchant in room {room_vnum}")
    debug_print(f" Room {room_vnum} exists: {room_vnum in rooms}")
    
    merchant_names = ["Mysterious Trader", "Wandering Merchant", "Exotic Vendor", "Traveling Salesman", "Mystic Peddler"]
    merchant_name = random.choice(merchant_names)
    
    active_events[room_vnum] = {
        'type': 'merchant',
        'data': {
            'name': merchant_name,
            'items': merchant_items.copy()
        },
        'duration': 300  # 5 minutes
    }
    
    debug_print(f" Merchant '{merchant_name}' spawned in room {room_vnum}")
    debug_print(f" Active events now: {list(active_events.keys())}")
    
    # Notify players in the room
    if room_vnum in rooms:
        room = rooms[room_vnum]
        # Find players currently in this room
        players_in_room = [p for p in players.values() if p.current_room.vnum == room_vnum]
        debug_print(f" Room has {len(players_in_room)} players")
        for player in players_in_room:
            send_to_player(player, f"🚚 {merchant_name} has set up shop here with exotic wares! 🚚\n")
            send_to_player(player, "Type 'list' to see what they're selling!\n")

def create_portal_storm():
    """Create temporary portals linking distant rooms"""
    room_vnums = list(rooms.keys())
    if len(room_vnums) < 2:
        return
    
    # Create 1-3 portal pairs
    num_portals = random.randint(1, 3)
    created_portals = []
    
    for _ in range(num_portals):
        # Pick two random rooms
        room1, room2 = random.sample(room_vnums, 2)
        
        # Don't overwrite existing events
        if room1 in active_events or room2 in active_events:
            continue
            
        # Create bidirectional portals
        end_time = time.time() + random.randint(120, 300)  # 2-5 minutes
        
        portal_data = {
            'destination': room2,
            'color': random.choice(['shimmering blue', 'crackling purple', 'golden', 'silver', 'emerald green'])
        }
        
        active_events[room1] = {
            'type': 'portal',
            'data': portal_data,
            'end_time': end_time
        }
        
        # Reverse portal
        reverse_portal_data = {
            'destination': room1,
            'color': portal_data['color']
        }
        
        active_events[room2] = {
            'type': 'portal',
            'data': reverse_portal_data,
            'end_time': end_time
        }
        
        # Set up portal connections
        portal_connections[room1] = room2
        portal_connections[room2] = room1
        
        created_portals.append((room1, room2))
        
        # Notify players in both rooms
        for room_vnum in [room1, room2]:
            if room_vnum in rooms:
                room = rooms[room_vnum]
                players_in_room = [p for p in players.values() if p.current_room.vnum == room_vnum]
                for player in players_in_room:
                    send_to_player(player, f"⚡ A {portal_data['color']} portal suddenly opens here! ⚡\n")
    
    if created_portals:
        print(f"Portal storm created {len(created_portals)} portal pairs: {created_portals}")

def create_monster_invasion():
    """Create an invasion of monsters in a random room"""
    room_vnums = list(rooms.keys())
    if not room_vnums:
        return
        
    target_room_vnum = random.choice(room_vnums)
    
    # Don't overwrite existing events
    if target_room_vnum in active_events:
        return
    
    invasion_types = {
        "Shadow Wraiths": {
            "keywords": ["shadow", "wraith", "wraiths"],
            "short_desc": "a shadow wraith",
            "long_desc": "A shadow wraith hovers here menacingly.",
            "description": "This ghostly figure seems to be made of pure darkness and malevolence.",
            "level": 8,
            "hp": 60,
            "count": random.randint(2, 4)
        },
        "Goblin Raiders": {
            "keywords": ["goblin", "raider", "raiders"],
            "short_desc": "a goblin raider",
            "long_desc": "A fierce goblin raider stands here, weapons drawn.",
            "description": "This small but vicious creature carries crude but deadly weapons.",
            "level": 5,
            "hp": 40,
            "count": random.randint(3, 5)
        },
        "Orc Warband": {
            "keywords": ["orc", "warrior", "warband"],
            "short_desc": "an orc warrior",
            "long_desc": "A brutal orc warrior stands ready for battle.",
            "description": "This massive green-skinned brute is covered in scars and armor.",
            "level": 7,
            "hp": 80,
            "count": random.randint(2, 3)
        },
        "Undead Horde": {
            "keywords": ["undead", "zombie", "skeleton"],
            "short_desc": "a shambling undead",
            "long_desc": "A rotting undead creature stumbles about here.",
            "description": "This once-living being now serves as a mindless puppet of dark magic.",
            "level": 6,
            "hp": 50,
            "count": random.randint(3, 6)
        }
    }
    
    invasion_name = random.choice(list(invasion_types.keys()))
    invasion_data = invasion_types[invasion_name]
    intensity = random.randint(1, 3)
    
    # Create invasion event
    end_time = time.time() + random.randint(300, 600)  # 5-10 minutes
    active_events[target_room_vnum] = {
        'type': 'invasion',
        'data': {
            'invasion_name': invasion_name,
            'intensity': intensity,
            'monsters': []  # Track spawned monsters
        },
        'end_time': end_time
    }
    
    # Spawn actual monsters in the room
    if target_room_vnum in rooms:
        room = rooms[target_room_vnum]
        
        # Spawn monsters based on intensity
        monster_count = invasion_data['count'] * intensity
        for i in range(monster_count):
            # Create a unique vnum for each monster (using negative numbers to avoid conflicts)
            monster_vnum = -(10000 + target_room_vnum * 100 + i)
            
            # Create the monster
            monster = Mobile(
                vnum=monster_vnum,
                keywords=invasion_data['keywords'],
                short_desc=invasion_data['short_desc'],
                long_desc=invasion_data['long_desc'],
                description=invasion_data['description'],
                level=invasion_data['level'],
                is_npc=False  # Make them hostile/attackable
            )
            
            # Set monster stats
            monster.hp = invasion_data['hp']
            monster.max_hp = invasion_data['hp']
            monster.current_hp = invasion_data['hp']
            monster.attack_power = invasion_data['level'] * 3
            monster.defense = invasion_data['level'] * 2
            monster.current_room = room
            
            # Add monster to room
            room.mobs.append(monster)
            
            # Track the monster in the invasion event
            active_events[target_room_vnum]['data']['monsters'].append(monster)
        
        # Notify players in the room
        players_in_room = [p for p in players.values() if p.current_room.vnum == target_room_vnum]
        for player in players_in_room:
            send_to_player(player, f"🗡️ This area is under attack by {invasion_name}! 🗡️\n")
            send_to_player(player, f"You see {monster_count} hostile creatures materializing!\n")
            # Update room description
            player.describe_current_room()
    
    print(f"Monster invasion created: {invasion_name} ({monster_count} monsters) in room {target_room_vnum}")

def trigger_random_event():
    """Randomly trigger one of the available world events"""
    events = [
        (0.3, create_portal_storm),    # 30% chance
        (0.2, create_monster_invasion), # 20% chance
        (0.5, lambda: spawn_merchant_event(random.choice(list(rooms.keys())) if rooms else 2203))  # 50% chance
    ]
    
    # Weighted random selection
    total_weight = sum(weight for weight, _ in events)
    random_value = random.random() * total_weight
    
    current_weight = 0
    for weight, event_func in events:
        current_weight += weight
        if random_value <= current_weight:
            try:
                event_func()
            except Exception as e:
                print(f"Error triggering random event: {e}")
            break

def cleanup_expired_events():
    """Remove expired events from the world"""
    current_time = time.time()
    expired_events = []
    
    for room_vnum, event_data in active_events.items():
        if 'end_time' in event_data and current_time >= event_data['end_time']:
            expired_events.append(room_vnum)
    
    for room_vnum in expired_events:
        event = active_events[room_vnum]
        del active_events[room_vnum]
        
        # Clean up portal connections
        if event['type'] == 'portal':
            if room_vnum in portal_connections:
                dest_room = portal_connections[room_vnum]
                if dest_room in portal_connections:
                    del portal_connections[dest_room]
                del portal_connections[room_vnum]
        
        # Clean up invasion monsters
        elif event['type'] == 'invasion':
            if room_vnum in rooms and 'monsters' in event['data']:
                room = rooms[room_vnum]
                invasion_monsters = event['data']['monsters']
                
                # Remove all invasion monsters from the room
                for monster in invasion_monsters:
                    if monster in room.mobs:
                        room.mobs.remove(monster)
                        
                print(f"Cleaned up {len(invasion_monsters)} invasion monsters from room {room_vnum}")
        
        # Notify players in room that event ended
        if room_vnum in rooms:
            room = rooms[room_vnum]
            players_in_room = [p for p in players.values() if p.current_room.vnum == room_vnum]
            for player in players_in_room:
                if event['type'] == 'portal':
                    send_to_player(player, "⚡ The portal shimmers and fades away. ⚡\n")
                elif event['type'] == 'invasion':
                    send_to_player(player, f"🗡️ The {event['data']['invasion_name']} retreat from this area. 🗡️\n")
                elif event['type'] == 'merchant':
                    send_to_player(player, f"🚚 {event['data']['name']} packs up and leaves. 🚚\n")
    
    if expired_events:
        print(f"Cleaned up {len(expired_events)} expired events: {expired_events}")

def world_events_loop():
    """Main loop for processing world events"""
    while True:
        try:
            # Clean up expired events
            cleanup_expired_events()
            
            # Randomly trigger new events
            if random.random() < 0.1:  # 10% chance per cycle
                trigger_random_event()
            
            time.sleep(30)  # Check every 30 seconds
            
        except Exception as e:
            print(f"World events loop error: {e}")
            import traceback
            traceback.print_exc()
            time.sleep(60)

def random_events():
    global current_weather, current_time_of_day
    if random.random() < 0.1:
        current_weather = random.choice(weather_conditions)
    if random.random() < 0.05:
        current_time_of_day = 'night' if current_time_of_day == 'day' else 'day'
    
    # Legacy merchant spawning (now handled by world_events_loop)
    if random.random() < 0.02:  # 2% chance
        room_vnums = list(rooms.keys())
        if room_vnums:
            random_room = random.choice(room_vnums)
            if random_room not in active_events:  # Don't overlap events
                spawn_merchant_event(random_room)

def spawn_wandering_trader(room):
    trader = Mobile(
        vnum=9999,
        keywords=['trader', 'merchant'],
        short_desc='a wandering trader',
        long_desc='A wandering trader is here, offering goods.',
        description='A trader who roams the lands selling rare items.',
        level=5,
        is_npc=True,
        inventory=[],
        room_vnum=room.vnum
    )
    room.mobs.append(trader)

def spawn_ambush_mobs(room):
    mob = Mobile(
        vnum=8888,
        keywords=['bandit'],
        short_desc='a sneaky bandit',
        long_desc='A sneaky bandit hides in the shadows.',
        description='A bandit looking for unsuspecting victims.',
        level=3,
        is_npc=False,
        inventory=[],
        room_vnum=room.vnum
    )
    room.mobs.append(mob)

def unlock_achievement(name, player):
    achievement = achievements.get(name)
    if achievement and not achievement.is_unlocked:
        achievement.is_unlocked = True
        player.achievements.append(achievement)
        send_to_player(player, f"Achievement Unlocked: {achievement.name}!\n")

achievements = {
    'First Blood': Achievement('First Blood', 'Defeat your first enemy.'),
    'Level 10': Achievement('Level 10', 'Reach level 10.'),
    'Treasure Hunter': Achievement('Treasure Hunter', 'Find a rare treasure.'),
    'Pet Tamer': Achievement('Pet Tamer', 'Successfully tame a creature.'),
    'Master Crafter': Achievement('Master Crafter', 'Successfully craft an item.'),
    'Explorer': Achievement('Explorer', 'Visit all rooms in the game.'),
    'Lucky Find': Achievement('Lucky Find', 'Discovered a hidden treasure while exploring.')
}

# Lucky Find System - Surprise & Delight Feature
lucky_find_treasures = [
    Object(vnum=9001, keywords=["coin", "shimmering"], short_desc="Shimmering Coin",
           long_desc="A coin that glows with inner light", description="A coin that glows with inner light",
           item_type="treasure", effects={"value": 100}),
    Object(vnum=9002, keywords=["crystal", "shard"], short_desc="Crystal Shard",
           long_desc="A beautiful crystal fragment pulsing with magic", description="A beautiful crystal fragment pulsing with magic",
           item_type="treasure", effects={"value": 250}),
    Object(vnum=9003, keywords=["key", "ancient"], short_desc="Ancient Key",
           long_desc="An ornate key covered in mysterious runes", description="An ornate key covered in mysterious runes",
           item_type="treasure", effects={"value": 500}),
    Object(vnum=9004, keywords=["feather", "golden"], short_desc="Golden Feather",
           long_desc="A magnificent feather that seems to dance in the air", description="A magnificent feather that seems to dance in the air",
           item_type="treasure", effects={"value": 300}),
    Object(vnum=9005, keywords=["fragment", "star"], short_desc="Star Fragment",
           long_desc="A tiny piece of a fallen star, warm to the touch", description="A tiny piece of a fallen star, warm to the touch",
           item_type="treasure", effects={"value": 750}),
    Object(vnum=9006, keywords=["ember", "phoenix"], short_desc="Phoenix Ember",
           long_desc="A glowing ember that never burns out", description="A glowing ember that never burns out",
           item_type="treasure", effects={"value": 1000}),
]

def trigger_lucky_find(player):
    """Handle lucky find events with configurable chance"""
    config = load_config()
    if not config.get('game', {}).get('surprise_events_enabled', True):
        return False

    lucky_chance = config.get('game', {}).get('lucky_find_chance', 0.05)

    if random.random() < lucky_chance:
        treasure = random.choice(lucky_find_treasures)
        treasure_copy = Object(
            vnum=treasure.vnum,
            keywords=treasure.keywords,
            short_desc=treasure.short_desc,
            long_desc=treasure.long_desc,
            description=treasure.description,
            item_type=treasure.item_type,
            effects=treasure.effects
        )

        # Ensure inventory is a list (safety check)
        if not isinstance(player.inventory, list):
            player.inventory = list(player.inventory) if hasattr(player.inventory, '__iter__') else []

        player.inventory.append(treasure_copy)

        # Exciting messages for different treasures
        messages = {
            "Shimmering Coin": "✨ As you explore, something catches your eye! You discover a Shimmering Coin hidden in a crevice!",
            "Crystal Shard": "💎 Your foot kicks something hard - a beautiful Crystal Shard emerges from the ground!",
            "Ancient Key": "🗝️  While looking around, you notice an Ancient Key partially buried in the dirt!",
            "Golden Feather": "🪶 A gentle breeze reveals a Golden Feather floating down from nowhere!",
            "Star Fragment": "⭐ Something twinkles at your feet - you've found a genuine Star Fragment!",
            "Phoenix Ember": "🔥 The air shimmers and a Phoenix Ember materializes before you!"
        }

        message = messages.get(treasure.short_desc, f"🎉 Lucky you! You found a {treasure.short_desc}!")
        send_to_player(player, f"\n{Colors.YELLOW}{message}{Colors.RESET}\n")

        unlock_achievement('Lucky Find', player)
        unlock_achievement('Treasure Hunter', player)

        # Broadcast to room (optional)
        broadcast_room(player.current_room, f"{player.name} looks excited about something they found!", exclude=player)

        return True
    return False

def give_daily_bonus(player):
    """Give player daily login bonus - surprise feature"""
    import datetime

    config = load_config()
    if not config.get('game', {}).get('daily_bonus_enabled', True):
        return

    # Check if player has last_login_date attribute
    if not hasattr(player, 'last_login_date'):
        player.last_login_date = None

    today = datetime.date.today()

    # Give bonus if it's a new day
    if player.last_login_date != today:
        player.last_login_date = today

        # Calculate bonus based on level
        bonus_gold = 50 + (player.level * 10)
        bonus_exp = 25 + (player.level * 5)

        # Add to player
        player.experience += bonus_exp

        # Create a gold coin item
        gold_bonus = Object(
            vnum=9100,
            keywords=["bonus", "gold", "bag"],
            short_desc=f"Daily Bonus ({bonus_gold} gold)",
            long_desc=f"A bag containing {bonus_gold} pieces of gold - your daily login reward!",
            description=f"A bag containing {bonus_gold} pieces of gold - your daily login reward!",
            item_type="treasure",
            effects={"value": bonus_gold}
        )
        player.inventory.append(gold_bonus)

        # Special messages for different days
        day_messages = [
            "🌅 Rise and shine! Here's your daily adventure bonus!",
            "⭐ The stars have aligned! Your dedication is rewarded!",
            "🎁 A mysterious benefactor has left you a gift!",
            "💎 Your persistence in this realm has been noticed!",
            "🏆 Champions like you deserve daily recognition!",
            "🌟 Another day, another opportunity for greatness!",
            "🎉 Your loyalty to the realm is greatly appreciated!"
        ]

        message = random.choice(day_messages)
        send_to_player(player, f"\n{Colors.YELLOW}{message}{Colors.RESET}\n")
        send_to_player(player, f"{Colors.GREEN}Daily Bonus: +{bonus_exp} XP and {bonus_gold} gold!{Colors.RESET}\n")

        # Check for level up
        check_level_up(player)

        # Chance for extra surprise
        if random.random() < 0.1:  # 10% chance
            extra_item = random.choice(lucky_find_treasures)
            extra_copy = Object(
                vnum=extra_item.vnum,
                keywords=extra_item.keywords,
                short_desc=extra_item.short_desc,
                long_desc=extra_item.long_desc,
                description=extra_item.description,
                item_type=extra_item.item_type,
                effects=extra_item.effects
            )
            player.inventory.append(extra_copy)
            send_to_player(player, f"{Colors.YELLOW}✨ BONUS SURPRISE: You also found a {extra_item.short_desc}!{Colors.RESET}\n")

crafting_recipes = {
    ('healing', 'herb'): {
        'result': 'potion of healing',
        'vnum': 6001
    }
}

def send_to_player(player, message):
    """Send a message to a player using their connection handler"""
    # Only send messages to actual Player objects, not Mobile objects
    if not isinstance(player, Player):
        return

    # Debug output for web players
    if hasattr(player, 'connection_handler') and isinstance(player.connection_handler, WebConnectionHandler):
        print(f"DEBUG SEND: Routing message to web player {player.name}: {message.strip()}")

    if hasattr(player, 'connection_handler') and player.connection_handler:
        player.connection_handler.send_message(message)
    elif hasattr(player, 'client_socket') and player.client_socket:
        # Fallback for backward compatibility
        try:
            player.client_socket.sendall(message.encode('utf-8'))
        except (ConnectionResetError, BrokenPipeError, OSError):
            print(f"Connection lost for player {player.name}")

def broadcast_room(room, message, exclude=None):
    for p_name, p in players.items():
        if p.current_room == room and p is not exclude:
            send_to_player(p, message)

def find_mob_in_room(room, mob_name):
    mob_name = mob_name.lower()
    for mob in room.mobs:
        if mob_name in [kw.lower() for kw in mob.keywords]:
            return mob
    return None

def find_target_in_room(room, target_name):
    target_name = target_name.lower()
    
    # Check other players first
    for p_name, pl in players.items():
        if pl.current_room == room and pl.name.lower() == target_name:
            return pl
    
    # Check mobs by keywords (exact match)
    for mob in room.mobs:
        if hasattr(mob, 'keywords') and mob.keywords:
            if target_name in [kw.lower() for kw in mob.keywords]:
                return mob
    
    # Check mobs by keywords (partial match)
    for mob in room.mobs:
        if hasattr(mob, 'keywords') and mob.keywords:
            for keyword in mob.keywords:
                if target_name in keyword.lower() or keyword.lower() in target_name:
                    return mob
    
    # Check mobs by short description (partial match)
    for mob in room.mobs:
        if hasattr(mob, 'short_desc') and mob.short_desc:
            # Remove the ~ character and check
            short_desc_clean = mob.short_desc.replace('~', '').lower()
            if target_name in short_desc_clean or any(word in short_desc_clean for word in target_name.split()):
                return mob
    
    return None


def check_level_up(player):
    required_xp = player.level * 100
    if player.experience >= required_xp:
        player.level += 1
        player.skill_points += 5
        send_to_player(player, f"You have leveled up to level {player.level}!\n")
        send_to_player(player, "You have gained 5 skill points to allocate.\n")
        player.max_hp = player.calculate_max_hp()
        player.hp = player.max_hp
        player.max_mana = player.calculate_max_mana()
        player.mana = player.max_mana
        player.attack_power = player.calculate_attack_power()
        player.defense = player.calculate_defense()
        if player.level == 10:
            unlock_achievement('Level 10', player)

def process_player_command(player, command):
    parts = command.split()  # Always define parts first

    # Safety check: ensure inventory is always a list
    if not isinstance(player.inventory, list):
        player.inventory = list(player.inventory) if hasattr(player.inventory, '__iter__') else []

    if command in command_abbreviations:
        command = command_abbreviations[command]
    else:
        if parts and parts[0] in command_abbreviations:
            parts[0] = command_abbreviations[parts[0]]
            command = ' '.join(parts)

    if command in ['north', 'south', 'east', 'west', 'up', 'down']:
        player.move(command)
    elif command.startswith('attack'):
        parts = command.split()
        if len(parts) == 2:
            target_name = parts[1]
            target = find_target_in_room(player.current_room, target_name)
            if target is None:
                send_to_player(player, "No such target.\n")
            else:
                # Check if already in combat with this target
                if in_combat(player):
                    send_to_player(player, "You are already in combat!\n")
                else:
                    # Start combat - the combat loop will handle the attacks
                    start_combat(player, target)
                    send_to_player(player, f"You engage {get_target_name(target)} in combat!\n")
                    broadcast_room(player.current_room, f"{player.name} attacks {get_target_name(target)}!\n", exclude=[player])
        else:
            # Attack first hostile mob?
            mobs = [m for m in player.current_room.mobs if not m.is_npc]
            if mobs:
                mob = mobs[0]
                if in_combat(player):
                    send_to_player(player, "You are already in combat!\n")
                else:
                    # Start combat - the combat loop will handle the attacks
                    start_combat(player, mob)
                    send_to_player(player, f"You engage {get_target_name(mob)} in combat!\n")
                    broadcast_room(player.current_room, f"{player.name} attacks {get_target_name(mob)}!\n", exclude=[player])
            else:
                # Attack a player if present?
                # There's no specified 'hostile' player by default.
                send_to_player(player, "Attack who?\n")
    elif command == 'special':
        # simplified: use special as a stronger attack
        mobs = [m for m in player.current_room.mobs if not m.is_npc]
        other_players = [p for p_name,p in players.items() if p != player and p.current_room == player.current_room]
        if mobs:
            mob = mobs[0]
            damage = max(1, (player.attack_power + player.level * 2) - mob.defense)
            mob.hp -= damage
            send_to_player(player, f"You unleash a powerful strike dealing {damage} damage to {mob.short_desc}!\n")
            if mob.hp <= 0:
                handle_defeat(player, mob)
            else:
                player_attack(mob, player)
        elif other_players:
            target = other_players[0]
            damage = max(1, (player.attack_power + player.level * 2) - target.defense)
            target.hp -= damage
            send_to_player(player, f"You unleash a powerful strike dealing {damage} damage to {target.name}!\n")
            send_to_player(target, f"{player.name} strikes you with a special attack for {damage} damage!\n")
            if target.hp <= 0:
                handle_defeat(player, target)
        else:
            send_to_player(player, "There is no enemy to use 'special' on.\n")

    elif command == 'look':
        player.describe_current_room()
        # Chance for lucky find when exploring
        trigger_lucky_find(player)
    elif command.startswith('cast '):
        parts = command.split()
        if len(parts) < 2:
            send_to_player(player, "Cast what spell?\n")
            return
        
        spell_name = parts[1]
        target = None
        
        # Check if a target was specified
        if len(parts) >= 3:
            target_name = ' '.join(parts[2:])
            target = find_target_in_room(player.current_room, target_name)
            if not target:
                send_to_player(player, f"You don't see '{target_name}' here.\n")
                return
        
        # Check if player knows the spell (allow partial matching)
        spell = None
        spell_key = None
        spell_name_lower = spell_name.lower()
        
        # First try exact match
        if spell_name_lower in player.spellbook:
            spell = player.spellbook[spell_name_lower]
            spell_key = spell_name_lower
        else:
            # Try partial matching
            for known_spell_key in player.spellbook:
                if spell_name_lower in known_spell_key or any(word.startswith(spell_name_lower) for word in known_spell_key.split()):
                    spell = player.spellbook[known_spell_key]
                    spell_key = known_spell_key
                    break
        
        if not spell:
            send_to_player(player, f"You don't know the spell '{spell_name}'.\n")
            return
        
        # Check if player has enough mana
        if player.mana < spell.mana_cost:
            send_to_player(player, f"You don't have enough mana to cast {spell.name}. (Need {spell.mana_cost}, have {player.mana})\n")
            return
        
        # Check if spell requires target and none was provided
        if spell.requires_target and not target:
            # Auto-target combat opponent if player is in combat
            if in_combat(player):
                combat_opponent = find_combat_opponent(player)
                if combat_opponent:
                    target = combat_opponent
                    send_to_player(player, f"Auto-targeting {target.short_desc}.\n")
                else:
                    send_to_player(player, f"The spell '{spell.name}' requires a target, but no combat opponent found.\n")
                    return
            else:
                send_to_player(player, f"The spell '{spell.name}' requires a target.\n")
                return
        
        # Check if spell doesn't require target but one was provided
        if not spell.requires_target and target:
            send_to_player(player, f"The spell '{spell.name}' doesn't require a target.\n")
            return
        
        # Cast the spell
        player.mana -= spell.mana_cost
        send_to_player(player, f"You cast {spell.name}!\n")
        
        # Apply spell effects based on spell type
        if spell.spell_type == 'offensive':
            if target:
                damage = random.randint(spell.base_damage[0], spell.base_damage[1])
                damage = int(damage * spell.damage_multiplier)
                
                if hasattr(target, 'hp'):
                    target.hp -= damage
                elif hasattr(target, 'current_hp'):
                    target.current_hp -= damage
                
                send_to_player(player, f"Your {spell.name} hits {get_target_name(target)} for {damage} damage!\n")
                
                # Notify target if it's a player
                if isinstance(target, Player):
                    send_to_player(target, f"{player.name}'s {spell.name} hits you for {damage} damage!\n")
                
                # Start combat if target is still alive
                target_hp = getattr(target, 'hp', getattr(target, 'current_hp', 0))
                if target_hp > 0:
                    # Start combat between player and target
                    start_combat(player, target)
                    
                    # If target is a mob and still alive, it should retaliate
                    if isinstance(target, Mobile) and not target.is_npc:
                        player_attack(target, player)
                else:
                    # Target died from the spell
                    send_to_player(player, f"Your spell defeats {get_target_name(target)}!\n")
                    
                    # Give experience and handle death
                    if hasattr(player, 'experience') and hasattr(target, 'level'):
                        base_xp = target.level * 20
                        player.experience += base_xp
                        send_to_player(player, f"You gain {base_xp} experience points.\n")
                        check_level_up(player)
                    
                    # Remove dead mob from room
                    if hasattr(target, 'is_npc') or not hasattr(target, 'name'):
                        if hasattr(player, 'current_room') and player.current_room and hasattr(player.current_room, 'mobs'):
                            if target in player.current_room.mobs:
                                player.current_room.mobs.remove(target)
        
        elif spell.spell_type == 'area_offensive':
            # Area of effect spell like Chain Lightning
            targets = []
            for mob in player.current_room.mobs:
                if not getattr(mob, 'is_npc', False):  # Only target combat mobs, not NPCs
                    targets.append(mob)
            
            if not targets:
                send_to_player(player, f"Your {spell.name} crackles through the air but finds no targets!\n")
            else:
                damage = random.randint(spell.base_damage[0], spell.base_damage[1])
                damage = int(damage * spell.damage_multiplier)
                
                send_to_player(player, f"Your {spell.name} arcs through the room!\n")
                
                surviving_targets = []
                for target in targets:
                    if hasattr(target, 'hp'):
                        target.hp -= damage
                    elif hasattr(target, 'current_hp'):
                        target.current_hp -= damage
                    
                    send_to_player(player, f"Lightning strikes {get_target_name(target)} for {damage} damage!\n")
                    
                    # Check if target died
                    target_hp = getattr(target, 'hp', getattr(target, 'current_hp', 0))
                    if target_hp <= 0:
                        send_to_player(player, f"Your spell defeats {get_target_name(target)}!\n")
                        
                        # Give experience and handle death
                        if hasattr(player, 'experience') and hasattr(target, 'level'):
                            base_xp = target.level * 20
                            player.experience += base_xp
                        
                        # Remove dead mob from room
                        if hasattr(target, 'is_npc') or not hasattr(target, 'name'):
                            if hasattr(player, 'current_room') and player.current_room and hasattr(player.current_room, 'mobs'):
                                if target in player.current_room.mobs:
                                    player.current_room.mobs.remove(target)
                    else:
                        # Target survived, add to combat
                        surviving_targets.append(target)
                
                # Start combat with all surviving targets
                for target in surviving_targets:
                    start_combat(player, target)
                
                # Have surviving mobs retaliate
                if surviving_targets:
                    # Pick one random surviving target to attack back immediately
                    retaliating_target = random.choice(surviving_targets)
                    if isinstance(retaliating_target, Mobile) and not retaliating_target.is_npc:
                        player_attack(retaliating_target, player)
        
        elif spell.spell_type == 'healing':
            # Use base_heal if available, otherwise fall back to base_damage
            heal_range = getattr(spell, 'base_heal', spell.base_damage)
            heal_multiplier = getattr(spell, 'heal_multiplier', spell.damage_multiplier)
            
            heal_amount = random.randint(heal_range[0], heal_range[1])
            heal_amount = int(heal_amount * heal_multiplier)
            
            old_hp = player.hp
            player.hp = min(player.max_hp, player.hp + heal_amount)
            actual_heal = player.hp - old_hp
            
            send_to_player(player, f"Your {spell.name} restores {actual_heal} hit points!\n")
            send_to_player(player, f"You now have {player.hp}/{player.max_hp} hit points.\n")
    
    elif command == 'spells':
        send_to_player(player, "Your Spellbook:\n")
        if player.spellbook:
            for spell_name, spell in player.spellbook.items():
                mana_cost = spell.mana_cost
                description = spell.description
                send_to_player(player, f"  {spell.name} (Cost: {mana_cost} mana) - {description}\n")
        else:
            send_to_player(player, "You don't know any spells yet.\n")
    
    elif command.startswith('learn '):
        parts = command.split()
        if len(parts) < 2:
            send_to_player(player, "Learn what spell?\n")
            return
        
        spell_name = parts[1].lower()
        if spell_name in spells:
            if spell_name in player.spellbook:
                send_to_player(player, f"You already know {spells[spell_name].name}.\n")
            else:
                player.spellbook[spell_name] = spells[spell_name]
                send_to_player(player, f"You learn {spells[spell_name].name}!\n")
        else:
            send_to_player(player, f"There is no spell called '{spell_name}'.\n")
    
    elif command.startswith('get '):
        item_name = command[4:]
        found = False
        for obj in player.current_room.objects:
            if any(item_name in kw for kw in obj.keywords):
                player.pick_up(obj)
                player.current_room.objects.remove(obj)
                found = True
                break
        if not found:
            send_to_player(player, "There is no such item here.\n")
    elif command == 'inventory':
        player.show_inventory()
    elif command == 'stats':
        player.show_stats()
    elif command == 'skills':
        player.show_skills()
    elif command.startswith('allocate '):
        parts = command.split()
        if len(parts) == 3 and parts[1] in ['strength', 'agility', 'intelligence', 'vitality']:
            try:
                points = int(parts[2])
                player.allocate_skill_points(parts[1], points)
            except ValueError:
                send_to_player(player, "Please specify a valid number of points.\n")
        else:
            send_to_player(player, "Usage: allocate <skill> <points>\n")
    elif command == 'rest':
        player.rest()
    elif command == 'stand':
        player.stand()
    elif command.startswith('teleport '):
        room_identifier = command[9:]
        player.teleport(room_identifier)
    elif command == 'map':
        player.show_map()
    elif command == 'save':
        save_game()
        send_to_player(player, "Game saved successfully.\n")
    elif command == 'load':
        load_game(player)
    elif command == 'help':
        show_help(player)
    elif command.startswith('craft '):
        parts = command.split()
        if len(parts) == 3:
            player.craft_item(parts[1], parts[2])
        else:
            send_to_player(player, "Usage: craft <item1> <item2>\n")
    elif command == 'quests':
        show_quests(player)
    elif command == 'achievements':
        player.view_achievements()
    elif command.startswith('talk '):
        if len(parts) >= 2:
            npc_name = ' '.join(parts[1:])
            talk_to_npc(player, npc_name)
        else:
            send_to_player(player, "Talk to whom?\n")
    elif command == 'stop':
        # End chat session in current room
        room_vnum = player.current_room.vnum
        if room_vnum in chat_sessions:
            del chat_sessions[room_vnum]
            send_to_player(player, f"{Colors.YELLOW}You end the conversation. NPCs return to their normal activities.{Colors.RESET}\n")
            broadcast_room(player.current_room, f"{Colors.YELLOW}{player.name} ends the conversation.{Colors.RESET}\n", exclude=player)
        else:
            send_to_player(player, "There is no active conversation to stop.\n")
    elif command.startswith('chat '):
        message = ' '.join(parts[1:]) if len(parts) > 1 else ''
        if message:
            chat_message = f"[CHAT] {player.name}: {message}"
            with players_lock:
                for other_player in players.values():
                    send_to_player(other_player, chat_message)
        else:
            send_to_player(player, "Usage: chat <message>\n")
    
    elif command.startswith('say '):
        message = command[4:].strip()
        if not message:
            send_to_player(player, "What do you want to say?\n")
            return False
        
        room_vnum = player.current_room.vnum
        
        # Check if there's an ongoing chat session in this room
        if room_vnum not in chat_sessions:
            # No active chat session, just broadcast normally
            broadcast_room(player.current_room, f"{Colors.GREEN}{player.name}: {message}{Colors.RESET}\n")
            return False
        
        # There's an active chat session, use LLM for NPC responses
        chat_data = chat_sessions[room_vnum]
        room_npcs = chat_data.get('npcs', [])
        npc = room_npcs[0] if room_npcs else None
        
        if not npc:
            # No NPCs to respond, just broadcast
            broadcast_room(player.current_room, f"{Colors.GREEN}{player.name}: {message}{Colors.RESET}\n")
            return False
        
        # Broadcast the player's message
        broadcast_room(player.current_room, f"{Colors.GREEN}{player.name}: {message}{Colors.RESET}\n")
        conversation_history = chat_data.get('conversation', [])
        
        # Add the player's message to the conversation history
        conversation_history.append({"role": "user", "content": message})
        
        # Limit conversation history to the last 6 exchanges
        if len(conversation_history) > 6:
            conversation_history = [conversation_history[0]] + conversation_history[-5:]
        
        # Generate responses from all NPCs in the room
        if len(room_npcs) == 1:
            # Single NPC response
            ai_reply = llm_chat(conversation_history)
            broadcast_room(player.current_room, f"{Colors.BLUE}{npc.short_desc}: {ai_reply}{Colors.RESET}\n", exclude=None)
            conversation_history.append({"role": "assistant", "content": ai_reply})
        else:
            # Multiple NPCs respond
            # Randomly select 1-3 NPCs to respond (not all at once to avoid spam)
            responding_npcs = random.sample(room_npcs, min(random.randint(1, 3), len(room_npcs)))
            
            for responding_npc in responding_npcs:
                # Create a modified prompt for this specific NPC
                npc_specific_history = conversation_history[:]
                if len(npc_specific_history) > 0:
                    # Modify the system prompt to focus on this NPC
                    original_system = npc_specific_history[0]['content']
                    npc_context = responding_npc.personality if responding_npc.personality else responding_npc.description
                    npc_specific_history[0] = {
                        "role": "system", 
                        "content": f"You are {responding_npc.short_desc} in a group conversation. Background: {npc_context[:200]}. Respond naturally as this character would in first person, keeping responses brief since others may also respond. Do not include your character name in the response."
                    }
                
                ai_reply = llm_chat(npc_specific_history)
                broadcast_room(player.current_room, f"{Colors.BLUE}{responding_npc.short_desc}: {ai_reply}{Colors.RESET}\n", exclude=None)
                conversation_history.append({"role": "assistant", "content": f"[{responding_npc.short_desc}] {ai_reply}"})
        
        # Update conversation history  
        chat_data['conversation'] = conversation_history
        
        # Remind player how to continue the conversation
        send_to_player(player, f"{Colors.YELLOW}[Use 'say <message>' to continue talking]{Colors.RESET}\n")
        return False
    
    elif command == 'list':
        debug_print(f" Player {player.name} using 'list' command in room {player.current_room.vnum}")
        list_vendor_items(player)
    elif command.startswith('buy '):
        item_name = ' '.join(parts[1:])
        buy_from_vendor(player, item_name)
    elif command.startswith('sell '):
        item_name = ' '.join(parts[1:])
        sell_to_vendor(player, item_name)
    elif command.startswith('open '):
        direction = command[5:]
        open_door(player, direction)
    elif command.startswith('close '):
        direction = command[6:]
        close_door(player, direction)
    elif command.startswith('unlock '):
        parts = command.split()
        if len(parts) >= 3:
            direction = parts[1]
            code = parts[2]
            unlock_door(player, direction, code)
        elif len(parts) == 2:
            direction = parts[1]
            unlock_door(player, direction)
        else:
            send_to_player(player, "Usage: unlock <direction> [code]\n")
    elif command.startswith('equip '):
        item_name = ' '.join(parts[1:])
        equip_command(player, item_name)
    elif command.startswith('unequip '):
        item_name = ' '.join(parts[1:])
        unequip_command(player, item_name)
    elif command.startswith('summon '):
        mob_name = ' '.join(parts[1:])
        summon_command(player, mob_name)
    elif command.startswith('enter ') or command == 'enter':
        if command == 'enter':
            # Check if there's a portal here
            room_vnum = player.current_room.vnum
            if room_vnum in active_events and active_events[room_vnum]['type'] == 'portal':
                enter_portal(player)
            else:
                send_to_player(player, "Enter what? There's nothing here to enter.\n")
        else:
            target = ' '.join(parts[1:])
            if 'portal' in target.lower():
                enter_portal(player)
            else:
                send_to_player(player, f"You can't enter {target}.\n")
    elif command == 'who':
        who_command(player)
    elif command == 'merchant' and player.name.lower() == 'admin':  # Debug command
        spawn_merchant_event(player.current_room.vnum)
        send_to_player(player, "Merchant event spawned!\n")
    elif command == 'invasion' and player.name.lower() == 'admin':  # Debug command
        create_monster_invasion()
        send_to_player(player, "Monster invasion triggered!\n")
    elif command in ['flee', 'escape']:
        if in_combat(player):
            # Find combat partner
            opponent = find_combat_opponent(player)
            if opponent:
                stop_combat(player, opponent)
                send_to_player(player, "You flee from combat!\n")
                broadcast_room(player.current_room, f"{player.name} flees from combat!\n", exclude=[player])
            else:
                send_to_player(player, "You are no longer in combat.\n")
        else:
            send_to_player(player, "You are not in combat.\n")
    elif command == 'help':
        show_help(player)
    elif command in ['bonus', 'surprises', 'lucky']:
        show_surprise_status(player)
    elif command.startswith('use '):
        item_name = ' '.join(parts[1:])
        use_item(player, item_name)
    elif command == 'quit':
        send_to_player(player, "Goodbye!\n")
        player.connection_handler.close_connection()
        if player.name in players:
            del players[player.name]
    else:
        send_to_player(player, "Unknown command. Type 'help' to see a list of available commands.\n")

def show_help(player):
    send_to_player(player, "Available Commands:\n")
    send_to_player(player, "Movement: north, south, east, west, up, down\n")
    send_to_player(player, "Combat: attack <target>, flee/escape, special, rest, stand\n")
    send_to_player(player, "Character: inventory, stats, skills, allocate <skill> <points>, achievements\n")
    send_to_player(player, "Items: get <item>, equip <item>, unequip <item>, use <item>\n")
    send_to_player(player, "Magic: cast <spell> [target], spells, learn <spell>\n")
    send_to_player(player, "World: look, map, teleport <room>, craft <item1> <item2>, quests\n")
    send_to_player(player, "Social: chat <message>, talk <npc>, say <message>, who\n")
    send_to_player(player, "Trading: list (vendor items), buy <item>, sell <item>\n")
    send_to_player(player, "Doors: open <direction>, close <direction>, unlock <direction> [code]\n")
    send_to_player(player, "Special: enter portal, summon <mob>, bonus/surprises, stop (end conversation)\n")
    send_to_player(player, "System: help, quit\n")

def show_surprise_status(player):
    """Show player their surprise events and bonus status"""
    config = load_config()

    send_to_player(player, f"\n{Colors.YELLOW}=== SURPRISE REWARDS STATUS ==={Colors.RESET}\n")

    # Lucky Find System Status
    if config.get('game', {}).get('surprise_events_enabled', True):
        chance = config.get('game', {}).get('lucky_find_chance', 0.05) * 100
        send_to_player(player, f"🎁 Lucky Find System: {Colors.GREEN}ACTIVE{Colors.RESET}\n")
        send_to_player(player, f"   Chance per exploration: {chance}%\n")
        send_to_player(player, f"   Rewards: Magical treasures while looking around and moving\n")
    else:
        send_to_player(player, f"🎁 Lucky Find System: {Colors.RED}DISABLED{Colors.RESET}\n")

    # Daily Bonus Status
    if config.get('game', {}).get('daily_bonus_enabled', True):
        send_to_player(player, f"🌅 Daily Login Bonus: {Colors.GREEN}ACTIVE{Colors.RESET}\n")
        if hasattr(player, 'last_login_date'):
            import datetime
            if player.last_login_date == datetime.date.today():
                send_to_player(player, f"   Today's bonus: {Colors.GREEN}CLAIMED{Colors.RESET}\n")
            else:
                send_to_player(player, f"   Today's bonus: {Colors.YELLOW}AVAILABLE{Colors.RESET}\n")
        else:
            send_to_player(player, f"   Today's bonus: {Colors.YELLOW}AVAILABLE{Colors.RESET}\n")

        bonus_gold = 50 + (player.level * 10)
        bonus_exp = 25 + (player.level * 5)
        send_to_player(player, f"   Your level {player.level} bonus: {bonus_exp} XP + {bonus_gold} gold\n")
    else:
        send_to_player(player, f"🌅 Daily Login Bonus: {Colors.RED}DISABLED{Colors.RESET}\n")

    # Combat Speed
    combat_speed = config.get('game', {}).get('combat_round_interval', 2)
    send_to_player(player, f"⚔️  Combat Round Speed: {combat_speed} second(s) per round\n")

    # Achievement Status
    lucky_achievement = None
    for achievement in player.achievements:
        if achievement.name == 'Lucky Find':
            lucky_achievement = achievement
            break

    if lucky_achievement:
        send_to_player(player, f"🏆 Lucky Find Achievement: {Colors.GREEN}UNLOCKED{Colors.RESET}\n")
    else:
        send_to_player(player, f"🏆 Lucky Find Achievement: {Colors.YELLOW}Available to unlock{Colors.RESET}\n")

    send_to_player(player, f"\n{Colors.CYAN}Keep exploring to discover hidden treasures!{Colors.RESET}\n")

def show_quests(player):
    send_to_player(player, "Active Quests:\n")
    if player.quests:
        for quest in player.quests:
            status = "Completed" if quest.is_completed else "In Progress"
            send_to_player(player, f"- {quest.name} [{status}]\n")
            send_to_player(player, f"  {quest.description}\n")
            for obj in quest.objectives:
                if isinstance(obj, KillObjective):
                    send_to_player(player, f"  Objective: {obj.description} ({obj.current_kills}/{obj.required_kills})\n")
                elif isinstance(obj, CollectObjective):
                    send_to_player(player, f"  Objective: {obj.description} ({obj.current_amount}/{obj.required_amount})\n")
    else:
        send_to_player(player, "You have no active quests.\n")

def save_game():
    door_states = {}
    for room in rooms.values():
        for dir_num, exit_data in room.exits.items():
            if exit_data['door_flags'] in (1, 3):
                door_id = f"{room.vnum}-{dir_num}"
                door_states[door_id] = {
                    'is_open': exit_data['is_open'],
                    'is_locked': exit_data['is_locked']
                }

    with open('savegame.pkl', 'wb') as f:
        pickle.dump({'players': players, 'all_npcs': all_npcs, 'door_states': door_states}, f)

def load_game(player):
    try:
        with open('savegame.pkl', 'rb') as f:
            data = pickle.load(f)
            # This would replace global players and npcs, which might not be desired
            # For a real MUD, handle carefully. Here we skip loading players from save to avoid conflicts.
            door_states = data.get('door_states', {})
            for door_id, state in door_states.items():
                room_vnum, dir_num = map(int, door_id.split('-'))
                if room_vnum in rooms and dir_num in rooms[room_vnum].exits:
                    rooms[room_vnum].exits[dir_num]['is_open'] = state['is_open']
                    rooms[room_vnum].exits[dir_num]['is_locked'] = state['is_locked']
        send_to_player(player, "Game loaded successfully.\n")
        player.describe_current_room()
    except FileNotFoundError:
        send_to_player(player, "No saved game found.\n")

def player_login(client_socket):
    # Create telnet connection handler
    connection_handler = TelnetConnectionHandler(client_socket)
    
    connection_handler.send_message("Welcome to the MUD! Enter your character name: ")
    name = connection_handler.receive_line()
    
    if not name:
        name = "Player" + str(random.randint(1000,9999))
    
    if name in players:
        p = players[name]
        # Update connection handler for returning player
        p.connection_handler = connection_handler
        p.client_socket = client_socket  # Backward compatibility

        # CRITICAL FIX: Ensure returning player has valid HP
        if p.hp <= 0:
            print(f"WARNING: Returning player {p.name} had {p.hp} HP, setting to full health")
            p.hp = p.max_hp

        send_to_player(p, f"Welcome back, {p.name}!\n")

        # Check for daily bonus (surprise feature)
        give_daily_bonus(p)

        p.describe_current_room()
    else:
        # Create a new player
        start_room = 2201
        p = Player(name, start_room, connection_handler)
        players[name] = p
        
        # Load player profile if it exists
        load_player_profile(p)
        
        # Load some default spells
        default_spells = ['fireball', 'magic missile', 'heal', 'chain lightning']
        for spell_name in default_spells:
            if spell_name in spells:
                p.spellbook[spell_name] = spells[spell_name]
        
        send_to_player(p, f"Welcome, {p.name}! You appear in {p.current_room.name}.\n")
        p.describe_current_room()
    
    return p

def list_vendor_items(player):
    """Show items available for purchase from vendors in current room"""
    room = player.current_room
    vendors = [npc for npc in room.mobs if hasattr(npc, 'inventory') and npc.inventory and hasattr(npc, 'is_npc') and npc.is_npc]
    
    # Check for active merchant events
    has_merchant_event = (room.vnum in active_events and 
                         active_events[room.vnum].get('type') == 'merchant')
    
    # Debug output
    print(f"DEBUG LIST: Room {room.vnum}, Vendors: {len(vendors)}, Merchant event: {has_merchant_event}")
    print(f"DEBUG LIST: Active events: {list(active_events.keys())}")
    print(f"DEBUG LIST: Room Mobs: {len(room.mobs)}")
    
    if not vendors and not has_merchant_event:
        send_to_player(player, "There are no vendors here.\n")
        return
    
    send_to_player(player, "Items available for purchase:\n")
    
    # Show regular vendor items
    for vendor in vendors:
        send_to_player(player, f"\nFrom {vendor.short_desc}:\n")
        for i, item in enumerate(vendor.inventory, 1):
            price = calculate_item_price(item)
            send_to_player(player, f"{i}. {getattr(item, 'short_desc', 'unknown item')} - {price} gold\n")
    
    # Show traveling merchant items
    if has_merchant_event:
        event = active_events[room.vnum]
        merchant_name = event['data']['name']
        send_to_player(player, f"\nFrom {merchant_name}:\n")
        for i, item in enumerate(merchant_items, 1):
            price = calculate_item_price(item)
            send_to_player(player, f"{i}. {item.get('short_desc', 'unknown item')} - {price} gold\n")

def calculate_item_price(item):
    """Calculate the price of an item based on its properties"""
    base_price = 10
    item_type = item.get('item_type', 'misc')
    
    # Price modifiers by item type
    type_modifiers = {
        'weapon': 50,
        'armor': 40,
        'ring': 100,
        'amulet': 80,
        'scroll': 30,
        'potion': 20,
        'misc': 10
    }
    
    return type_modifiers.get(item_type, base_price)

def buy_from_vendor(player, item_name):
    """Buy an item from a vendor in the current room"""
    room = player.current_room
    vendors = [npc for npc in room.mobs if hasattr(npc, 'inventory') and npc.inventory and hasattr(npc, 'is_npc') and npc.is_npc]
    
    # Check for active merchant events
    has_merchant_event = (room.vnum in active_events and 
                         active_events[room.vnum].get('type') == 'merchant')
    
    if not vendors and not has_merchant_event:
        send_to_player(player, "There are no vendors here.\n")
        return
    
    # Initialize gold if needed
    if not hasattr(player, 'gold'):
        player.gold = 100  # Start with some gold
    
    # First try merchant event items
    if has_merchant_event:
        for item in merchant_items:
            item_keywords = item.get('keywords', [])
            item_short = item.get('short_desc', '').lower()
            
            if (item_name.lower() in [kw.lower() for kw in item_keywords] or 
                item_name.lower() in item_short):
                
                price = calculate_item_price(item)
                
                if player.gold < price:
                    send_to_player(player, f"You don't have enough gold! You need {price} gold but only have {player.gold}.\n")
                    return
                
                # Complete the transaction - create a copy of the item
                player.gold -= price
                item_copy = item.copy() if hasattr(item, 'copy') else dict(item)
                player.inventory.append(item_copy)
                
                send_to_player(player, f"You buy {item.get('short_desc', 'an item')} for {price} gold from the traveling merchant.\n")
                send_to_player(player, f"You have {player.gold} gold remaining.\n")
                return
    
    # Then try regular vendor items
    for vendor in vendors:
        for item in vendor.inventory[:]:  # Use slice to avoid modification during iteration
            item_keywords = item.get('keywords', [])
            item_short = item.get('short_desc', '').lower()
            
            if (item_name.lower() in [kw.lower() for kw in item_keywords] or 
                item_name.lower() in item_short):
                
                price = calculate_item_price(item)
                
                if player.gold < price:
                    send_to_player(player, f"You don't have enough gold! You need {price} gold but only have {player.gold}.\n")
                    return
                
                # Complete the transaction
                player.gold -= price
                player.inventory.append(item)
                vendor.inventory.remove(item)
                
                send_to_player(player, f"You buy {item.get('short_desc', 'an item')} for {price} gold.\n")
                send_to_player(player, f"You have {player.gold} gold remaining.\n")
                return
    
    send_to_player(player, f"No vendor here sells '{item_name}'.\n")

def open_door(player, direction):
    """Open a door in the specified direction"""
    dir_num = reverse_direction_map.get(direction)
    if dir_num is not None and dir_num in player.current_room.exits:
        exit_data = player.current_room.exits[dir_num]
        if exit_data['door_flags'] in (1, 3):
            if exit_data.get('is_open', False):
                send_to_player(player, "The door is already open.\n")
            else:
                if exit_data.get('is_locked', False):
                    send_to_player(player, "The door is locked. You need to unlock it first.\n")
                else:
                    send_to_player(player, "You open the door.\n")
                    exit_data['is_open'] = True
        else:
            send_to_player(player, "There is no door in that direction.\n")
    else:
        send_to_player(player, "You can't open that.\n")

def close_door(player, direction):
    """Close a door in the specified direction"""
    dir_num = reverse_direction_map.get(direction)
    if dir_num is not None and dir_num in player.current_room.exits:
        exit_data = player.current_room.exits[dir_num]
        if exit_data['door_flags'] in (1, 3):
            if not exit_data.get('is_open', False):
                send_to_player(player, "The door is already closed.\n")
            else:
                send_to_player(player, "You close the door.\n")
                exit_data['is_open'] = False
        else:
            send_to_player(player, "There is no door in that direction.\n")
    else:
        send_to_player(player, "You can't close that.\n")

def unlock_door(player, direction, code=None):
    """Unlock a door in the specified direction"""
    dir_num = reverse_direction_map.get(direction)
    if dir_num is not None and dir_num in player.current_room.exits:
        exit_data = player.current_room.exits[dir_num]
        if exit_data.get('is_locked', False):
            if 'secret_code' in exit_data and exit_data['secret_code']:
                if code == exit_data['secret_code']:
                    exit_data['is_locked'] = False
                    send_to_player(player, "You have unlocked the door.\n")
                else:
                    send_to_player(player, "Incorrect code. The door remains locked.\n")
            else:
                # No code required, just unlock
                exit_data['is_locked'] = False
                send_to_player(player, "You unlock the door.\n")
        else:
            send_to_player(player, "The door is not locked.\n")
    else:
        send_to_player(player, "There is no door in that direction.\n")

def who_command(player):
    """Show list of players currently online"""
    send_to_player(player, "Players Online:\n")
    with players_lock:
        for p_name, p in players.items():
            send_to_player(player, f"- {p_name}\n")

def talk_to_npc(player, npc_name):
    """Start or join a conversation with NPCs in the current room"""
    print(f"DEBUG CHAT: Player {player.name} attempting to talk to '{npc_name}'")
    room_vnum = player.current_room.vnum
    print(f"DEBUG CHAT: Player is in room {room_vnum}")
    
    # Find the specific NPC to start conversation with
    target_npc = None
    print(f"DEBUG CHAT: Searching for target NPC '{npc_name}' among {len(player.current_room.mobs)} mobs in room")
    
    for mob in player.current_room.mobs:
        print(f"DEBUG CHAT: Checking mob: {mob.short_desc}, is_npc: {getattr(mob, 'is_npc', False)}")
        if hasattr(mob, 'is_npc') and mob.is_npc:
            mob_keywords = getattr(mob, 'keywords', [])
            print(f"DEBUG CHAT: NPC {mob.short_desc} has keywords: {mob_keywords}")
            if npc_name.lower() in [k.lower() for k in mob_keywords] or npc_name.lower() in mob.short_desc.lower():
                target_npc = mob
                print(f"DEBUG CHAT: Found target NPC: {mob.short_desc}")
                break
    
    if not target_npc:
        print(f"DEBUG CHAT: No target NPC found for '{npc_name}'")
        send_to_player(player, f"There is no '{npc_name}' here to talk to.\n")
        return
    
    # Get ALL NPCs in the room for the conversation
    room_npcs = []
    for mob in player.current_room.mobs:
        if hasattr(mob, 'is_npc') and mob.is_npc:
            room_npcs.append(mob)
    
    print(f"DEBUG CHAT: Found {len(room_npcs)} NPCs in room for conversation: {[npc.short_desc for npc in room_npcs]}")
    
    # Initialize or update the chat session for this room
    if room_vnum not in chat_sessions:
        print(f"DEBUG CHAT: Creating new chat session for room {room_vnum}")
        
        # Create new chat session
        npc = target_npc  # Use the targeted NPC as primary speaker
        npc_context = npc.personality if npc.personality else npc.description
        npc_context = npc_context[:500]  # Limit context length
        
        print(f"DEBUG CHAT: Using primary NPC: {npc.short_desc}")
        print(f"DEBUG CHAT: NPC context (first 100 chars): {npc_context[:100]}...")
        
        # Set up conversation with system prompt that acknowledges multiple NPCs
        if len(room_npcs) == 1:
            system_prompt = f"You are {npc.short_desc}, an NPC in a text-based RPG. Background: {npc_context}. Always respond in first person without including your character name in responses."
        else:
            npc_names = [n.short_desc for n in room_npcs]
            system_prompt = f"You are {npc.short_desc}, an NPC in a text-based RPG with other NPCs present ({', '.join(npc_names)}). Background: {npc_context}. You may respond for yourself or facilitate group conversation. Always respond in first person without including your character name in responses."
        
        if hasattr(npc, 'background') and npc.background:
            system_prompt += f" Additional background: {npc.background[:200]}"
        if hasattr(npc, 'secrets') and npc.secrets:
            system_prompt += f" Secret knowledge: {npc.secrets[:200]}"
        
        print(f"DEBUG CHAT: System prompt: {system_prompt}")
        
        conversation_history = [{"role": "system", "content": system_prompt}]
        
        chat_sessions[room_vnum] = {
            'npcs': room_npcs,
            'players': [player],
            'conversation': conversation_history
        }
        
        print(f"DEBUG CHAT: Chat session created with {len(room_npcs)} NPCs and 1 player")
        
        # Get NPCs to greet the player
        npc_names = [npc.short_desc for npc in room_npcs]
        if len(npc_names) == 1:
            send_to_player(player, f"You start a conversation with {npc_names[0]}.\n")
        else:
            send_to_player(player, f"You start a group conversation with {', '.join(npc_names[:-1])} and {npc_names[-1]}.\n")
        
        # Have the primary NPC greet the player with AI
        print(f"DEBUG CHAT: Preparing AI greeting request for {npc.short_desc}")
        greeting_prompt = "A player approaches you to start a conversation. Greet them naturally and ask how you can help."
        greeting_request = conversation_history + [{"role": "user", "content": greeting_prompt}]
        print(f"DEBUG CHAT: Greeting request has {len(greeting_request)} messages")
        
        ai_reply = llm_chat(greeting_request)
        print(f"DEBUG CHAT: AI greeting reply received: '{ai_reply[:100]}...'")
        
        if ai_reply:
            # Broadcast NPC's initial response
            print(f"DEBUG CHAT: Broadcasting greeting to room: {npc.short_desc}: {ai_reply}")
            broadcast_room(player.current_room, f"{Colors.BLUE}{npc.short_desc}: {ai_reply}{Colors.RESET}\n", exclude=None)
            
            # Add the greeting exchange to history
            print(f"DEBUG CHAT: Adding greeting exchange to conversation history")
            chat_sessions[room_vnum]['conversation'].append({"role": "user", "content": "Hello"})
            chat_sessions[room_vnum]['conversation'].append({"role": "assistant", "content": ai_reply})
            print(f"DEBUG CHAT: Conversation history now has {len(chat_sessions[room_vnum]['conversation'])} messages")
        else:
            print(f"DEBUG CHAT: AI greeting failed, sending fallback message")
            broadcast_room(player.current_room, f"{Colors.BLUE}{npc.short_desc}: Hello there! How can I help you?{Colors.RESET}\n", exclude=None)
    else:
        print(f"DEBUG CHAT: Joining existing chat session in room {room_vnum}")
        chat_data = chat_sessions[room_vnum]
        print(f"DEBUG CHAT: Current session has {len(chat_data.get('players', []))} players and {len(chat_data.get('npcs', []))} NPCs")
        
        if player not in chat_data['players']:
            chat_data['players'].append(player)
            print(f"DEBUG CHAT: Added player {player.name} to existing session")
        
        if 'conversation' not in chat_data:
            print(f"DEBUG CHAT: No conversation history found, creating new system prompt")
            npc = target_npc
            npc_context = npc.personality if npc.personality else npc.description
            npc_context = npc_context[:500]
            print(f"DEBUG CHAT: Using NPC context for system prompt: {npc_context[:50]}...")
            
            if len(room_npcs) == 1:
                system_prompt = f"You are {npc.short_desc}, an NPC in a text-based RPG. Background: {npc_context}. Always respond in first person without including your character name in responses."
            else:
                npc_names = [n.short_desc for n in room_npcs]
                system_prompt = f"You are {npc.short_desc}, an NPC in a text-based RPG with other NPCs present ({', '.join(npc_names)}). Background: {npc_context}. You may respond for yourself or facilitate group conversation. Always respond in first person without including your character name in responses."
            
            print(f"DEBUG CHAT: Created system prompt for existing session: {system_prompt[:100]}...")
            conversation_history = [{"role": "system", "content": system_prompt}]
            chat_data['conversation'] = conversation_history
            print(f"DEBUG CHAT: Initialized conversation history with system prompt")
        else:
            print(f"DEBUG CHAT: Using existing conversation history with {len(chat_data['conversation'])} messages")
        
        # Update NPCs list to include all room NPCs
        chat_data['npcs'] = room_npcs
        print(f"DEBUG CHAT: Updated NPCs list to {len(room_npcs)} NPCs")
        send_to_player(player, f"You join the ongoing conversation.\n")
        broadcast_room(player.current_room, f"{player.name} joins the conversation.\n", exclude=player)
    
    # Inform how to continue
    send_to_player(player, f"{Colors.YELLOW}[Use 'say <message>' to continue talking]{Colors.RESET}\n")

def get_target_name(entity):
    """Get the name identifier for combat tracking"""
    if hasattr(entity, 'name') and not getattr(entity, 'is_npc', False):
        return entity.name
    else:
        return entity.short_desc

def start_combat(attacker, defender):
    """Start combat between two entities"""
    attacker_name = get_target_name(attacker)
    defender_name = get_target_name(defender)

    # Debug HP values when combat starts
    attacker_hp = getattr(attacker, 'hp', getattr(attacker, 'current_hp', 'UNKNOWN'))
    defender_hp = getattr(defender, 'hp', getattr(defender, 'current_hp', 'UNKNOWN'))
    attacker_level = getattr(attacker, 'level', 'UNKNOWN')
    defender_level = getattr(defender, 'level', 'UNKNOWN')

    print(f"DEBUG COMBAT: Starting combat - {attacker_name} (lvl {attacker_level}, {attacker_hp} HP) vs {defender_name} (lvl {defender_level}, {defender_hp} HP)")

    pair = tuple(sorted([attacker_name, defender_name]))
    combatants[pair] = True

def stop_combat(attacker, defender):
    """Stop combat between two entities"""
    pair = tuple(sorted([get_target_name(attacker), get_target_name(defender)]))
    if pair in combatants:
        del combatants[pair]

def in_combat(entity):
    """Check if entity is in combat"""
    n = entity.name if hasattr(entity, 'name') and not getattr(entity, 'is_npc', False) else entity.short_desc
    for pair in combatants:
        if n in pair:
            return True
    return False

def find_combat_opponent(entity):
    """Find the current combat opponent of an entity"""
    n = entity.name if hasattr(entity, 'name') and not getattr(entity, 'is_npc', False) else entity.short_desc
    for pair in combatants:
        if n in pair:
            names = list(pair)
            if names[0] == n:
                return find_any_entity_by_name(names[1], entity.current_room)
            else:
                return find_any_entity_by_name(names[0], entity.current_room)
    return None

def find_any_entity_by_name(name, room):
    """Find any entity (player or mob) by name in a room"""
    name_lower = name.lower()
    
    # Check players
    for p_name, p in players.items():
        if p.current_room == room and p.name.lower() == name_lower:
            return p
    
    # Check mobs
    for mob in room.mobs:
        if mob.short_desc.lower() == name_lower:
            return mob
    
    return None

def perform_special_attack(attacker, defender):
    """Perform a special attack with enhanced damage"""
    base_damage = random.randint(attacker.attack_power, attacker.attack_power * 2)
    special_multiplier = 2
    damage = max(1, base_damage * special_multiplier - defender.defense)
    
    defender.current_hp -= damage
    
    send_to_player(attacker, f"You perform a devastating special attack on {get_target_name(defender)} for {damage} damage!\n")
    
    # Only send message to defender if it's a player
    if isinstance(defender, Player):
        send_to_player(defender, f"{get_target_name(attacker)} unleashes a powerful special attack on you for {damage} damage!\n")
    
    # Notify others in room
    for p in players.values():
        if p.current_room == attacker.current_room and p != attacker and p != defender:
            send_to_player(p, f"{get_target_name(attacker)} performs a special attack on {get_target_name(defender)}!\n")
    
    return damage

def equip_command(player, item_name):
    """Equip an item from inventory"""
    if not item_name:
        send_to_player(player, "Equip what?\n")
        return
    
    # Find item in inventory
    item = None
    for it in player.inventory:
        try:
            # Handle both dict and object items
            if hasattr(it, 'keywords') and it.keywords and any(item_name.lower() in kw.lower() for kw in it.keywords):
                item = it
                break
            elif isinstance(it, dict) and 'keywords' in it and it['keywords'] and any(item_name.lower() in kw.lower() for kw in it['keywords']):
                item = it
                break
            elif hasattr(it, 'short_desc') and it.short_desc and item_name.lower() in it.short_desc.lower():
                item = it
                break
            elif isinstance(it, dict) and 'short_desc' in it and it['short_desc'] and item_name.lower() in it['short_desc'].lower():
                item = it
                break
        except (AttributeError, KeyError, TypeError) as e:
            # Skip items with malformed data
            print(f"Warning: Malformed item in inventory: {e}")
            continue
    
    if not item:
        send_to_player(player, "You don't have that item.\n")
        return

    # Determine appropriate slot based on item_type
    slot = None
    item_type = None
    if hasattr(item, 'item_type'):
        item_type = item.item_type
    elif isinstance(item, dict) and 'item_type' in item:
        item_type = item['item_type']
    
    if item_type:
        if item_type == 'weapon':
            slot = 'weapon'
        elif item_type == 'armor':
            slot = 'armor'
        elif item_type == 'shield':
            slot = 'shield'
        elif item_type == 'ring':
            slot = 'ring'
        elif item_type == 'amulet':
            slot = 'amulet'
    
    if not slot:
        send_to_player(player, "You cannot equip that item.\n")
        return

    # If slot is occupied, unequip first
    if player.equipment[slot]:
        unequipped_item = player.equipment[slot]
        player.equipment[slot] = None
        player.inventory.append(unequipped_item)
        unequipped_name = unequipped_item.get('short_desc', 'the item') if isinstance(unequipped_item, dict) else getattr(unequipped_item, 'short_desc', 'the item')
        send_to_player(player, f"You remove {unequipped_name}.\n")

    player.inventory.remove(item)
    player.equipment[slot] = item
    item_name = item.get('short_desc', 'the item') if isinstance(item, dict) else getattr(item, 'short_desc', 'the item')
    send_to_player(player, f"You equip {item_name}.\n")
    
    # Recalculate stats after equipment change
    player.attack_power = player.calculate_attack_power()
    player.defense = player.calculate_defense()

def unequip_command(player, item_name):
    """Unequip an item and put it in inventory"""
    if not item_name:
        send_to_player(player, "Unequip what?\n")
        return
    
    # Find equipped item
    item_to_unequip = None
    slot_to_clear = None
    
    for slot, item in player.equipment.items():
        if item and (
            (hasattr(item, 'keywords') and any(item_name.lower() in kw.lower() for kw in item.keywords)) or
            (isinstance(item, dict) and 'keywords' in item and any(item_name.lower() in kw.lower() for kw in item['keywords'])) or
            (hasattr(item, 'short_desc') and item_name.lower() in item.short_desc.lower()) or
            (isinstance(item, dict) and 'short_desc' in item and item_name.lower() in item['short_desc'].lower())
        ):
            item_to_unequip = item
            slot_to_clear = slot
            break
    
    if not item_to_unequip:
        send_to_player(player, "You don't have that item equipped.\n")
        return
    
    # Unequip the item
    player.equipment[slot_to_clear] = None
    player.inventory.append(item_to_unequip)
    item_name = item_to_unequip.get('short_desc', 'the item') if isinstance(item_to_unequip, dict) else getattr(item_to_unequip, 'short_desc', 'the item')
    send_to_player(player, f"You unequip {item_name}.\n")
    
    # Recalculate stats after equipment change
    player.attack_power = player.calculate_attack_power()
    player.defense = player.calculate_defense()

def use_item(player, item_name):
    """Use a consumable item from inventory"""
    if not item_name:
        send_to_player(player, "Use what?\n")
        return

    # Find item in inventory
    item_to_use = None
    item_index = None

    for i, item in enumerate(player.inventory):
        # Check if item matches (support for both Object instances and dictionaries)
        item_matches = False

        if hasattr(item, 'keywords'):  # Object instance
            item_matches = any(item_name.lower() in kw.lower() for kw in item.keywords)
        elif isinstance(item, dict) and 'keywords' in item:  # Dictionary
            item_matches = any(item_name.lower() in kw.lower() for kw in item['keywords'])

        if not item_matches:
            # Also check short_desc
            if hasattr(item, 'short_desc'):  # Object instance
                item_matches = item_name.lower() in item.short_desc.lower()
            elif isinstance(item, dict) and 'short_desc' in item:  # Dictionary
                item_matches = item_name.lower() in item['short_desc'].lower()

        if item_matches:
            item_to_use = item
            item_index = i
            break

    if not item_to_use:
        send_to_player(player, "You don't have that item.\n")
        return

    # Check if item is consumable (has effects)
    effects = None
    item_type = None

    if isinstance(item_to_use, dict):
        effects = item_to_use.get('effects')
        item_type = item_to_use.get('item_type')
        item_desc = item_to_use.get('short_desc', 'the item')
    else:
        # Object instance - check for effects attribute
        effects = getattr(item_to_use, 'effects', None)
        item_type = getattr(item_to_use, 'item_type', None)
        item_desc = getattr(item_to_use, 'short_desc', 'the item')

    if not effects:
        send_to_player(player, f"You cannot use {item_desc}.\n")
        return

    # Apply item effects
    used_successfully = False

    if 'heal' in effects:
        heal_amount = effects['heal']
        old_hp = player.hp
        player.hp = min(player.hp + heal_amount, player.max_hp)
        actual_heal = player.hp - old_hp
        send_to_player(player, f"You use {item_desc} and recover {actual_heal} health!\n")
        used_successfully = True

    if 'mana' in effects:
        mana_amount = effects['mana']
        old_mana = player.mana
        player.mana = min(player.mana + mana_amount, player.max_mana)
        actual_mana = player.mana - old_mana
        send_to_player(player, f"You use {item_desc} and recover {actual_mana} mana!\n")
        used_successfully = True

    if 'power' in effects:
        power_amount = effects['power']
        # Temporary power boost - could implement as temporary stat boost
        player.attack_power += power_amount
        send_to_player(player, f"You use {item_desc} and feel your power increase by {power_amount}!\n")
        used_successfully = True

    if 'magic' in effects:
        magic_amount = effects['magic']
        # Temporary magic boost - could implement as temporary stat boost
        if hasattr(player, 'magic_power'):
            player.magic_power += magic_amount
        else:
            player.magic_power = magic_amount
        send_to_player(player, f"You use {item_desc} and feel your magical power increase by {magic_amount}!\n")
        used_successfully = True

    # Remove the item from inventory if it was used successfully
    if used_successfully:
        player.inventory.pop(item_index)
        broadcast_room(player.current_room, f"{player.name} uses {item_desc}.\n", exclude=[player])
    else:
        send_to_player(player, f"You don't know how to use {item_desc}.\n")

def broadcast_room(room, message, exclude=None):
    """Send a message to all players in a room except excluded player"""
    with players_lock:
        players_list = list(players.items())
    for p_name, p in players_list:
        if p.current_room == room and p is not exclude:
            send_to_player(p, message + "\n")

def broadcast_all(message):
    """Send a message to all players"""
    with players_lock:
        players_list = list(players.items())
    for p_name, p in players_list:
        send_to_player(p, message + "\n")

def find_entity_globally(name):
    """Find a player or mob anywhere in the world by name"""
    name_lower = name.lower()
    
    # Check all players
    for p_name, p in players.items():
        if p.name.lower() == name_lower:
            return p
    
    # Check all rooms for a matching mob
    for room in rooms.values():
        for mob in room.mobs:
            if hasattr(mob, 'short_desc') and mob.short_desc.lower() == name_lower:
                return mob
            if hasattr(mob, 'keywords') and any(name_lower in kw.lower() for kw in mob.keywords):
                return mob
    
    return None

def player_attack(attacker, defender):
    """Execute an attack between two entities"""
    # Calculate base stats
    attack_power = getattr(attacker, 'attack_power', 10)
    defense = getattr(defender, 'defense', 5)
    attacker_level = getattr(attacker, 'level', 1)
    defender_level = getattr(defender, 'level', 1)
    
    # Roll for hit/miss (based on level difference and stats)
    hit_chance = 85 + (attacker_level - defender_level) * 5
    hit_chance = max(10, min(95, hit_chance))  # Clamp between 10-95%

    print(f"DEBUG HIT: {get_target_name(attacker)} (lvl {attacker_level}) vs {get_target_name(defender)} (lvl {defender_level}) - hit chance: {hit_chance}%")
    
    if random.randint(1, 100) > hit_chance:
        # Miss
        print(f"DEBUG MISS: {get_target_name(attacker)} missed {get_target_name(defender)} (hit chance was {hit_chance}%)")

        miss_messages = [
            f"You swing wildly but miss {get_target_name(defender)}!",
            f"Your attack goes wide of {get_target_name(defender)}!",
            f"{get_target_name(defender)} dodges your attack!",
            f"You lose your footing and miss {get_target_name(defender)}!"
        ]
        # Send personalized miss message to player attacker
        if hasattr(attacker, 'name') and not getattr(attacker, 'is_npc', False):
            attacker_msg = random.choice(miss_messages)
            send_to_player(attacker, f"{attacker_msg}\n")

        # Send personalized message to player defender
        if hasattr(defender, 'name') and not getattr(defender, 'is_npc', False):
            send_to_player(defender, f"{get_target_name(attacker)}'s attack misses you!\n")

        # Broadcast to everyone else in the room (excluding attacker and defender if they're players)
        exclude_list = []
        if hasattr(attacker, 'name') and not getattr(attacker, 'is_npc', False):
            exclude_list.append(attacker)
        if hasattr(defender, 'name') and not getattr(defender, 'is_npc', False):
            exclude_list.append(defender)

        broadcast_room(attacker.current_room, f"{get_target_name(attacker)} misses {get_target_name(defender)}!\n", exclude=exclude_list)
        return

    # Calculate damage
    base_damage = random.randint(1, attack_power)
    damage = max(1, base_damage - defense)
    
    # Apply damage
    defender_hp_before = getattr(defender, 'hp', getattr(defender, 'current_hp', 0))
    if hasattr(defender, 'hp'):
        defender.hp -= damage
    elif hasattr(defender, 'current_hp'):
        defender.current_hp -= damage

    defender_hp_after = getattr(defender, 'hp', getattr(defender, 'current_hp', 0))
    print(f"DEBUG DAMAGE: {get_target_name(defender)} HP: {defender_hp_before} -> {defender_hp_after} (damage: {damage})")
    
    # Send messages
    if hasattr(attacker, 'name') and not getattr(attacker, 'is_npc', False):
        # Player attacker gets personal message
        send_to_player(attacker, f"You hit {get_target_name(defender)} for {damage} damage!\n")
    
    if hasattr(defender, 'name') and not getattr(defender, 'is_npc', False):
        # Player defender gets personal message
        send_to_player(defender, f"{get_target_name(attacker)} hits you for {damage} damage!\n")
    
    # Broadcast to everyone else in the room (excluding attacker and defender if they're players)
    exclude_list = []
    if hasattr(attacker, 'name') and not getattr(attacker, 'is_npc', False):
        exclude_list.append(attacker)
    if hasattr(defender, 'name') and not getattr(defender, 'is_npc', False):
        exclude_list.append(defender)
    
    broadcast_room(attacker.current_room, f"{get_target_name(attacker)} hits {get_target_name(defender)} for {damage} damage!\n", exclude=exclude_list)
    
    # Check for death
    defender_hp = getattr(defender, 'hp', getattr(defender, 'current_hp', 0))
    if defender_hp <= 0:
        send_to_player(attacker, f"You have defeated {get_target_name(defender)}!\n")
        broadcast_room(attacker.current_room, f"{get_target_name(defender)} has been defeated!\n", exclude=[attacker])

        # CRITICAL FIX: Stop combat immediately when defender dies
        stop_combat(attacker, defender)

        # Remove dead mob from room
        if hasattr(defender, 'is_npc') or not hasattr(defender, 'name'):
            # Find the room where this mob is located (use attacker's current room)
            if hasattr(attacker, 'current_room') and attacker.current_room and hasattr(attacker.current_room, 'mobs'):
                if defender in attacker.current_room.mobs:
                    attacker.current_room.mobs.remove(defender)

def combat_round():
    """Process one round of combat for all active combatants"""
    to_remove = []
    if combatants:  # Only print if there are active combats
        print(f"DEBUG COMBAT: Processing {len(combatants)} active combats: {list(combatants.keys())}")

    for pair in list(combatants.keys()):
        name1, name2 = pair
        ent1 = find_entity_globally(name1)
        ent2 = find_entity_globally(name2)

        if ent1 is None or ent2 is None:
            print(f"DEBUG COMBAT: Removing combat pair {pair} - entity not found")
            to_remove.append(pair)
            continue

        # Check if entities are in the same room (improved room checking)
        if hasattr(ent1, 'current_room') and hasattr(ent2, 'current_room'):
            ent1_room = getattr(ent1, 'current_room', None)
            ent2_room = getattr(ent2, 'current_room', None)
            ent1_room_vnum = ent1_room.vnum if ent1_room and hasattr(ent1_room, 'vnum') else None
            ent2_room_vnum = ent2_room.vnum if ent2_room and hasattr(ent2_room, 'vnum') else None

            # Only remove combat if we can confirm they're actually in different rooms
            # If either room is None or invalid, let combat continue (they might be valid entities without proper room setup)
            if (ent1_room_vnum is not None and ent2_room_vnum is not None and
                ent1_room_vnum != ent2_room_vnum):
                print(f"DEBUG COMBAT: Removing combat pair {pair} - entities in different rooms ({ent1_room_vnum} vs {ent2_room_vnum})")
                to_remove.append(pair)
                continue

        # Check HP for both entities
        ent1_hp = getattr(ent1, 'hp', getattr(ent1, 'current_hp', 0))
        ent2_hp = getattr(ent2, 'hp', getattr(ent2, 'current_hp', 0))

        print(f"DEBUG COMBAT: HP Check - {name1}: {ent1_hp}, {name2}: {ent2_hp}")

        if ent1_hp <= 0 or ent2_hp <= 0:
            print(f"DEBUG COMBAT: Entity has 0 HP - removing from combat ({name1}: {ent1_hp}, {name2}: {ent2_hp})")
            to_remove.append(pair)
            continue

        # Execute attacks
        print(f"DEBUG COMBAT: {name1} attacks {name2}")
        player_attack(ent1, ent2)
        
        # Check if defender died
        ent2_hp = getattr(ent2, 'hp', getattr(ent2, 'current_hp', 0))
        if ent2_hp <= 0:
            print(f"DEBUG COMBAT: {name2} defeated, removing from combat")
            to_remove.append(pair)
            continue

        # Retaliation attack
        print(f"DEBUG COMBAT: {name2} attacks {name1}")
        player_attack(ent2, ent1)
        
        # Check if original attacker died
        ent1_hp = getattr(ent1, 'hp', getattr(ent1, 'current_hp', 0))
        if ent1_hp <= 0:
            print(f"DEBUG COMBAT: {name1} defeated, removing from combat")
            to_remove.append(pair)

    # Remove finished combats
    for pair in to_remove:
        if pair in combatants:
            del combatants[pair]

def combat_loop():
    """Main combat processing loop"""
    config = load_config()
    combat_interval = config.get('game', {}).get('combat_round_interval', 2)

    # Ensure minimum interval to prevent infinite loops
    if combat_interval <= 0:
        combat_interval = 1
        print(f"WARNING: combat_round_interval was {config.get('game', {}).get('combat_round_interval')}, using minimum of 1 second")

    while True:
        try:
            if combatants:
                combat_round()
            time.sleep(combat_interval)
        except Exception as e:
            print(f"Combat loop error: {e}")
            import traceback
            traceback.print_exc()
            time.sleep(5)

def npc_chat_loop():
    """Continuous NPC chat processing loop"""
    while True:
        try:
            if chat_sessions:
                for room_vnum, session_data in list(chat_sessions.items()):
                    # Check if session is still active and has NPCs
                    if 'npcs' in session_data and session_data['npcs']:
                        npcs = session_data['npcs']
                        conversation = session_data.get('conversation', [])
                        
                        # Only have NPCs chat if there are players in the room
                        active_players = []
                        for player_name, player in players.items():
                            if player.current_room.vnum == room_vnum:
                                active_players.append(player)
                        
                        if active_players and len(npcs) >= 1:
                            # NPCs will always respond when players are present (100% chance every cycle)
                            # Pick a random NPC to initiate conversation
                            speaking_npc = random.choice(npcs)
                            
                            # Create context-appropriate prompt
                            if len(conversation) < 3:
                                # Early in conversation - introduce yourself or ask questions
                                npc_prompt = [
                                    {"role": "system", "content": f"You are {speaking_npc.short_desc}. Start a casual conversation or ask the players something interesting. Keep it brief (1-2 sentences). Respond in first person without including your character name."},
                                    {"role": "user", "content": "Continue the conversation naturally"}
                                ]
                            else:
                                # Ongoing conversation - be more contextual
                                recent_conversation = conversation[-4:] if len(conversation) > 4 else conversation[1:]  # Skip system message
                                npc_prompt = recent_conversation + [
                                    {"role": "user", "content": f"You are {speaking_npc.short_desc}. Continue the conversation naturally. Keep response brief and in first person without including your character name."}
                                ]
                            
                            # Generate AI response
                            ai_reply = llm_chat(npc_prompt)
                            
                            # Broadcast NPC message to room
                            room = rooms.get(room_vnum)
                            if room:
                                broadcast_room(room, f"{Colors.BLUE}{speaking_npc.short_desc}: {ai_reply}{Colors.RESET}\n")
                                
                                # Add to conversation history
                                session_data['conversation'].append({"role": "assistant", "content": f"[{speaking_npc.short_desc}] {ai_reply}"})
                                
                                # Limit conversation history
                                if len(session_data['conversation']) > 12:
                                    session_data['conversation'] = [session_data['conversation'][0]] + session_data['conversation'][-8:]
            
            time.sleep(random.randint(15, 45))  # NPCs chat every 15-45 seconds
            
        except Exception as e:
            print(f"NPC chat loop error: {e}")
            import traceback
            traceback.print_exc()
            time.sleep(10)

def enter_portal(player):
    """Enter a portal to travel to another room"""
    room_vnum = player.current_room.vnum
    
    if room_vnum not in active_events or active_events[room_vnum]['type'] != 'portal':
        send_to_player(player, "There's no portal here to enter.\n")
        return
    
    event = active_events[room_vnum]
    destination_vnum = event['data']['destination']
    
    if destination_vnum not in rooms:
        send_to_player(player, "The portal leads nowhere... something went wrong!\n")
        return
    
    # Move player through portal
    old_room = player.current_room
    new_room = rooms[destination_vnum]
    
    # Remove from old room's player list (if it exists)
    if hasattr(old_room, 'players') and player in old_room.players:
        old_room.players.remove(player)
    
    # Move player
    player.current_room = new_room
    
    # Add to new room's player list (if it exists)
    if hasattr(new_room, 'players'):
        new_room.players.append(player)
    
    # Messages
    broadcast_room(old_room, f"⚡ {player.name} steps into the portal and vanishes! ⚡", exclude=player)
    send_to_player(player, f"⚡ You step through the {event['data']['color']} portal... ⚡\n")
    send_to_player(player, "You feel a rush of magical energy as you're transported!\n")
    
    # Show new room
    player.describe_current_room()
    broadcast_room(new_room, f"⚡ {player.name} emerges from a shimmering portal! ⚡", exclude=player)

def summon_command(player, mob_name):
    """Summon a mobile by name into the player's current room"""
    if not mob_name:
        send_to_player(player, "Summon what?\n")
        return
    
    target = find_entity_globally(mob_name)
    
    if target and hasattr(target, 'is_npc'):
        # Mob found in the world, move it
        if hasattr(target, 'current_room') and target.current_room and target in target.current_room.mobs:
            target.current_room.mobs.remove(target)
        target.current_room = player.current_room
        player.current_room.mobs.append(target)
        send_to_player(player, f"You chant ancient words, and {target.short_desc} appears before you!\n")
        broadcast_room(player.current_room, f"{player.name} summons {target.short_desc}!", exclude=player)
    else:
        # Try to find a mob template to create a new one
        mob_name_lower = mob_name.lower()
        found_template = None
        for vnum, mob_template in mobiles.items():
            if (hasattr(mob_template, 'short_desc') and mob_name_lower in mob_template.short_desc.lower()) or \
               (hasattr(mob_template, 'keywords') and any(mob_name_lower in kw.lower() for kw in mob_template.keywords)):
                found_template = mob_template
                break

        if found_template:
            new_mob = copy.deepcopy(found_template)
            new_mob.current_room = player.current_room
            player.current_room.mobs.append(new_mob)
            send_to_player(player, f"You chant ancient words, and {new_mob.short_desc} appears before you!\n")
            broadcast_room(player.current_room, f"{player.name} summons {new_mob.short_desc}!", exclude=player)
        else:
            send_to_player(player, f"You cannot find '{mob_name}' to summon.\n")

def sell_to_vendor(player, item_name):
    """Sell an item to a vendor in the current room"""
    room = player.current_room
    vendors = [npc for npc in room.mobs if hasattr(npc, 'inventory') and hasattr(npc, 'is_npc') and npc.is_npc]
    
    if not vendors:
        send_to_player(player, "There are no vendors here to sell to.\n")
        return
    
    # Find item in player's inventory
    item_to_sell = None
    for item in player.inventory:
        item_keywords = item.get('keywords', [])
        item_short = item.get('short_desc', '').lower()
        
        if (item_name.lower() in [kw.lower() for kw in item_keywords] or 
            item_name.lower() in item_short):
            item_to_sell = item
            break
    
    if not item_to_sell:
        send_to_player(player, f"You don't have '{item_name}' in your inventory.\n")
        return
    
    # Calculate sell price (typically half of buy price)
    sell_price = calculate_item_price(item_to_sell) // 2
    
    # Complete the transaction
    if not hasattr(player, 'gold'):
        player.gold = 0
    
    player.gold += sell_price
    player.inventory.remove(item_to_sell)
    
    # Add to first vendor's inventory
    vendors[0].inventory.append(item_to_sell)
    
    send_to_player(player, f"You sell {item_to_sell.get('short_desc', 'an item')} for {sell_price} gold.\n")
    send_to_player(player, f"You now have {player.gold} gold.\n")

def handle_client(client_socket):
    player = player_login(client_socket)
    if not player:
        return
    
    try:
        send_to_player(player, "Type 'help' for commands.\n")
        
        while player.connection_handler.is_connected():
            send_to_player(player, "> ")
            command = player.connection_handler.receive_line()
            
            if command is None:  # Connection lost
                break
            
            command = command.strip().lower()
            if not command:
                continue
                
            # Check for quit command
            if command in ['quit', 'exit', 'bye']:
                send_to_player(player, "Goodbye!\n")
                break
                
            # Process the command
            try:
                should_disconnect = process_player_command(player, command)
                if should_disconnect:
                    break
            except Exception as e:
                send_to_player(player, f"Error processing command: {str(e)}\n")
                print(f"Error processing command '{command}' for {player.name}: {e}")
    
    except Exception as e:
        print(f"Error in handle_client for {player.name if player else 'unknown'}: {e}")
    
    finally:
        # Client disconnected - cleanup
        if player and player.name in players:
            print(f"Player {player.name} disconnected")
            # Save player profile before removing
            save_player_profile(player)
            # Remove from room players list
            if hasattr(player, 'current_room') and hasattr(player.current_room, 'players'):
                if player in player.current_room.players:
                    player.current_room.players.remove(player)
            # Clean up chat sessions if player was the only participant
            if hasattr(player, 'current_room'):
                room_vnum = player.current_room.vnum
                if room_vnum in chat_sessions:
                    session = chat_sessions[room_vnum]
                    if 'players' in session and player in session['players']:
                        session['players'].remove(player)
                    # If no players left, remove the session
                    if not session.get('players'):
                        del chat_sessions[room_vnum]
            
            # Remove from players dict
            if player.name in players:
                del players[player.name]
            # Close connection
            player.connection_handler.close_connection()

def run_server(host='0.0.0.0', port=9000):
    global server_socket
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    
    try:
        server_socket.bind((host, port))
        server_socket.listen(5)
        print(f"Server running on {host}:{port}...")
        
        while not shutdown_event.is_set():
            try:
                server_socket.settimeout(1.0)  # Allow periodic shutdown checks
                client_socket, addr = server_socket.accept()
                print(f"Connection from {addr}")
                t = threading.Thread(target=handle_client, args=(client_socket,))
                t.start()
            except socket.timeout:
                continue  # Check shutdown event
            except socket.error:
                if shutdown_event.is_set():
                    break
                raise
    except Exception as e:
        print(f"Server error: {e}")
    finally:
        if server_socket:
            server_socket.close()

player_spells = ['fireball', 'magic missile', 'heal']

# Global server state for shutdown
server_socket = None
web_thread = None
shutdown_event = threading.Event()

def signal_handler(signum, frame):
    """Handle SIGINT and SIGTERM for clean shutdown"""
    print("\nShutting down server...")
    shutdown_event.set()
    
    # Save all players
    with players_lock:
        for player in players.values():
            save_player_profile(player)
    
    # Close server socket
    if server_socket:
        server_socket.close()
    
    print("Server shutdown complete.")
    sys.exit(0)

if __name__ == "__main__":
    # Parse command line arguments
    parser = argparse.ArgumentParser(description='PyMUD3 - Multi-User Dungeon Game Server')
    parser.add_argument('--debug', action='store_true', help='Enable debug output')
    args = parser.parse_args()

    # Set global debug flag
    DEBUG = args.debug

    # Setup signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Initialize game data
    parse_area_file('area.txt')
    load_objects_from_file('objects.json')
    process_resets()
    place_random_treasures()
    load_spells_from_file('spells.json')
    load_npcs_from_file('npcs.json')

    # Start web interface
    try:
        import simple_web as integrated_web
        integrated_web.set_mud_module(sys.modules[__name__])
        web_thread = integrated_web.start_web_interface()
        if web_thread:
            print("Web interface available at: http://localhost:8080")
        else:
            print("Failed to start web interface")
    except ImportError:
        print("Web interface not available - integrated_web.py not found")

    # Start NPC movement
    npc_thread = threading.Thread(target=npc_movement_loop, daemon=True)
    npc_thread.start()
    
    # Start combat loop
    combat_thread = threading.Thread(target=combat_loop, daemon=True)
    combat_thread.start()
    
    # Start world events loop
    events_thread = threading.Thread(target=world_events_loop, daemon=True)
    events_thread.start()
    
    # Start NPC chat loop
    npc_chat_thread = threading.Thread(target=npc_chat_loop, daemon=True)
    npc_chat_thread.start()
    
    print("Dynamic world events system started!")
    print("Combat system started!")
    print("NPC continuous chat system started!")
    print("Advanced events system (portals, invasions) started!")

    # Create a test merchant in room 2203 for immediate testing
    spawn_merchant_event(2203)
    print("Test merchant spawned in room 2203!")

    # Start telnet server with port 4001 to avoid conflict
    run_server(port=4001)

