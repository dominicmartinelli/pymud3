import argparse
import copy
import json
import os
import pickle
import queue
import random
import re
import sys
import threading
import time
import traceback
import textwrap
import socket

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
        self.current_room = rooms.get(room_vnum)
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
        effect_func = None
        if spell_data['name'].lower() == 'fireball':
            effect_func = fireball_effect
        elif spell_data['name'].lower() == 'magic missile':
            effect_func = magic_missile_effect
        elif spell_data['name'].lower() == 'heal':
            effect_func = heal_effect
        else:
            continue
        spell = Spell(
            name=spell_data['name'],
            description=spell_data['description'],
            effect_func=effect_func,
            mana_cost=spell_data['mana_cost']
        )
        spells[spell.name.lower()] = spell

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
    def __init__(self, name, current_room_vnum, client_socket):
        self.name = name
        self.current_room = rooms[current_room_vnum]
        self.client_socket = client_socket
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
            'ring': None,
            'amulet': None
        }
        self.resting = False
        self.rest_thread = None
        self.status_effects = []
        self.spellbook = {}
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

    def pick_up(self, obj):
        self.inventory.append(obj)
        send_to_player(self, f"You picked up {obj.short_desc}.\n")
        # Check achievements, update quests, etc.

    def show_inventory(self):
        send_to_player(self, "Inventory:\n")
        if self.inventory:
            for item in self.inventory:
                send_to_player(self, f"- {item.short_desc}\n")
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
                send_to_player(self, f"  {slot.capitalize()}: {item.short_desc}\n")
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
            self.rest_thread.join()
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

def random_events():
    global current_weather, current_time_of_day
    if random.random() < 0.1:
        current_weather = random.choice(weather_conditions)
    if random.random() < 0.05:
        current_time_of_day = 'night' if current_time_of_day == 'day' else 'day'
    # Optional random events

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
    'Explorer': Achievement('Explorer', 'Visit all rooms in the game.')
}

crafting_recipes = {
    ('healing', 'herb'): {
        'result': 'potion of healing',
        'vnum': 6001
    }
}

def send_to_player(player, message):
    if player.client_socket:
        player.client_socket.sendall(message.encode('utf-8'))

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
    # Check other players
    for p_name, pl in players.items():
        if pl.current_room == room and pl.name.lower() == target_name:
            return pl
    # Check mobs
    for mob in room.mobs:
        if target_name in [kw.lower() for kw in mob.keywords]:
            return mob
    return None

def player_attack(attacker, defender):
    # attacker and defender can be Player or Mobile
    attack_power = attacker.attack_power if isinstance(attacker, Player) else attacker.attack_power
    defense = defender.defense if isinstance(defender, Player) else defender.defense
    damage = max(1, random.randint(attack_power - 5, attack_power + 5) - defense)
    defender.hp -= damage

    if isinstance(attacker, Player):
        send_to_player(attacker, f"You deal {damage} damage to {get_target_name(defender)}.\n")
    if isinstance(defender, Player):
        send_to_player(defender, f"{attacker.name} deals {damage} damage to you!\n")

    if defender.hp <= 0:
        handle_defeat(attacker, defender)

def get_target_name(entity):
    if isinstance(entity, Player):
        return entity.name
    else:
        return entity.short_desc

def handle_defeat(attacker, defender):
    if isinstance(defender, Player):
        # Player defeated
        msg = f"{defender.name} has been defeated by {get_target_name(attacker)}!\n"
        broadcast_room(defender.current_room, msg)
        # Handle respawn or death
        # Simple respawn:
        defender.hp = defender.max_hp
        defender.mana = defender.max_mana
        # Send them to a safe room
        safe_room_vnum = 2201
        if safe_room_vnum in rooms:
            defender.current_room = rooms[safe_room_vnum]
            send_to_player(defender, "You have been resurrected in the starting room.\n")
            defender.describe_current_room()
        # Adjust karma or loot drops if desired
    else:
        # Mob defeated
        if isinstance(attacker, Player):
            send_to_player(attacker, f"You have defeated {defender.short_desc}!\n")
            attacker.experience += defender.level * 20
            unlock_achievement('First Blood', attacker)
            defender.current_room.mobs.remove(defender)
            check_level_up(attacker)

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
    if command in command_abbreviations:
        command = command_abbreviations[command]
    else:
        parts = command.split()
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
                # Player attacks target
                player_attack(player, target)
                if target.hp > 0 and isinstance(target, Player):
                    # Target retaliates if player? Up to you. For now, no automatic retaliation.
                    pass
                elif target.hp > 0 and isinstance(target, Mobile) and not target.is_npc:
                    # Mobs retaliate
                    player_attack(target, player)
        else:
            # Attack first hostile mob?
            mobs = [m for m in player.current_room.mobs if not m.is_npc]
            if mobs:
                mob = mobs[0]
                player_attack(player, mob)
                if mob.hp > 0:
                    player_attack(mob, player)
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
        send_to_player(player, "NPC interaction not fully adapted for multiplayer in this example.\n")
    elif command == 'quit':
        send_to_player(player, "Goodbye!\n")
        player.client_socket.close()
        del players[player.name]
    else:
        send_to_player(player, "Unknown command. Type 'help' to see a list of available commands.\n")

def show_help(player):
    send_to_player(player, "Available Commands:\n")
    send_to_player(player, "Movement: north, south, east, west, up, down\n")
    send_to_player(player, "attack <target>, special, rest, stand, inventory, stats, skills, allocate <skill> <points>\n")
    send_to_player(player, "get <item>, look, map, teleport <room>, craft <item1> <item2>, quests, achievements\n")
    send_to_player(player, "help, quit\n")

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
    client_socket.sendall(b"Welcome to the MUD! Enter your character name: ")
    name = client_socket.recv(1024).decode('utf-8').strip()
    if not name:
        name = "Player" + str(random.randint(1000,9999))
    if name in players:
        p = players[name]
        p.client_socket = client_socket
        send_to_player(p, f"Welcome back, {p.name}!\n")
        p.describe_current_room()
    else:
        # Create a new player
        start_room = 2201
        p = Player(name, start_room, client_socket)
        players[name] = p
        # Load some spells if desired:
        for spell_name in player_spells:
            if spell_name in spells:
                p.spellbook[spell_name] = spells[spell_name]
        send_to_player(p, f"Welcome, {p.name}! You appear in {p.current_room.name}.\n")
        p.describe_current_room()
    return p

def handle_client(client_socket):
    player = player_login(client_socket)
    send_to_player(player, "Type 'help' for commands.\n")
    while True:
        send_to_player(player, "> ")
        data = client_socket.recv(1024)
        if not data:
            break
        command = data.decode('utf-8').strip().lower()
        if not command:
            continue
        process_player_command(player, command)

    # Client disconnected
    if player.name in players:
        del players[player.name]

def run_server(host='0.0.0.0', port=9000):
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.bind((host, port))
    server_socket.listen(5)
    print(f"Server running on {host}:{port}...")
    while True:
        client_socket, addr = server_socket.accept()
        print(f"Connection from {addr}")
        t = threading.Thread(target=handle_client, args=(client_socket,))
        t.start()

player_spells = ['fireball', 'magic missile', 'heal']

if __name__ == "__main__":
    parse_area_file('area.txt')
    load_objects_from_file('objects.json')
    process_resets()
    place_random_treasures()
    load_spells_from_file('spells.json')
    load_npcs_from_file('npcs.json')

    npc_thread = threading.Thread(target=npc_movement_loop, daemon=True)
    npc_thread.start()

    run_server()

