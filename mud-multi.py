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
import requests
import signal
from abc import ABC, abstractmethod
import secrets

# Note: Web interface is now handled by integrated_web.py

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

# Ensure player_saves directory exists
if not os.path.exists('player_saves'):
    os.makedirs('player_saves')

# Global variables for game state
server_running = True  # Controls the server loop
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
chat_sessions = {}  # Key: room_vnum, Value: {'npc': NPC object, 'player': Player object}

players = {}  # Key: player name, Value: Player object
players_lock = threading.Lock()  # Thread-safe access to players dictionary
web_players_registry = {}  # Key: player name, Value: WebPlayer object (for combat system)

class Colors:
    RESET = '\x1b[0m'
    RED = '\x1b[31m'
    GREEN = '\x1b[32m'
    YELLOW = '\x1b[33m'
    BLUE = '\x1b[34m'
    MAGENTA = '\x1b[35m'
    CYAN = '\x1b[36m'
    BOLD = '\x1b[1m'
    WHITE = '\x1b[37m'

class Room:
    def __init__(self, vnum, name, description, exits):
        self.vnum = vnum
        self.name = name
        self.description = description
        self.exits = exits
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
        self.room_vnum = room_vnum
        self.current_room = None  # Will be set during loading
        self.conversation_history = []
        self.has_given_items = False
        self.quest = None
        self.hp = self.level * 0.5
        self.max_hp = self.hp
        self.defense = self.level * 1.2
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
    def __init__(self, name, description, mana_cost, spell_type, requires_target=False, 
                 damage_multiplier=0, base_damage=None, heal_multiplier=0, base_heal=None):
        self.name = name
        self.description = description
        self.mana_cost = mana_cost
        self.spell_type = spell_type
        self.requires_target = requires_target
        self.damage_multiplier = damage_multiplier
        self.base_damage = base_damage or [0, 0]
        self.heal_multiplier = heal_multiplier  
        self.base_heal = base_heal or [0, 0]
    
    def cast(self, caster, target=None):
        """Execute the spell effect"""
        if self.spell_type == "offensive":
            return self._cast_offensive(caster, target)
        elif self.spell_type == "healing":
            return self._cast_healing(caster, target)
        elif self.spell_type == "area_offensive":
            return self._cast_area_offensive(caster)
        else:
            send_to_player(caster, "Unknown spell type!\n")
            return False
    
    def _cast_offensive(self, caster, target):
        if not target:
            send_to_player(caster, f"You need a target to cast {self.name}.\n")
            return False
        
        damage = caster.intelligence * self.damage_multiplier + random.randint(self.base_damage[0], self.base_damage[1])
        target.hp = max(0, target.hp - damage)
        
        if self.name.lower() == "fireball":
            send_to_player(caster, f"{Colors.RED}You hurl a fireball at {target.short_desc}, dealing {damage} damage!{Colors.RESET}\n")
        elif self.name.lower() == "magic missile":
            send_to_player(caster, f"{Colors.BLUE}Magic missiles strike {target.short_desc}, dealing {damage} damage!{Colors.RESET}\n")
        else:
            send_to_player(caster, f"{Colors.YELLOW}Your {self.name} hits {target.short_desc} for {damage} damage!{Colors.RESET}\n")
        
        if target.hp <= 0:
            send_to_player(caster, f"{Colors.GREEN}{target.short_desc} has been defeated by your spell!{Colors.RESET}\n")
        
        return True
    
    def _cast_healing(self, caster, target=None):
        heal_amount = caster.intelligence * self.heal_multiplier + random.randint(self.base_heal[0], self.base_heal[1])
        caster.hp = max(0, min(caster.max_hp, caster.hp + heal_amount))
        send_to_player(caster, f"{Colors.GREEN}You cast {self.name} on yourself, restoring {heal_amount} HP.{Colors.RESET}\n")
        return True
    
    def _cast_area_offensive(self, caster):
        """Cast area-of-effect offensive spell hitting all enemies in room"""
        room = caster.current_room
        targets = []
        
        # Find all potential targets (combat mobs and other players, but not NPCs or the caster)
        for mob in room.mobs:
            # Exclude NPCs (conversational characters) from combat spells
            if not (hasattr(mob, 'is_npc') and mob.is_npc):
                targets.append(mob)
        
        # Check if room has players list and iterate through it
        if hasattr(room, 'players') and room.players:
            for player in room.players:
                if player != caster:
                    targets.append(player)
        
        if not targets:
            send_to_player(caster, f"There are no targets in the room for {self.name}.\n")
            return False
        
        # Calculate damage once
        damage = caster.intelligence * self.damage_multiplier + random.randint(self.base_damage[0], self.base_damage[1])
        
        # Apply damage to all targets
        hit_targets = []
        defeated_targets = []
        
        for target in targets:
            target.hp = max(0, target.hp - damage)
            hit_targets.append(target)
            if target.hp <= 0:
                defeated_targets.append(target)
        
        # Send messages
        if self.name.lower() == "chain lightning":
            send_to_player(caster, f"{Colors.CYAN}Crackling arcs of electricity surge from your hands, striking all enemies in the room for {damage} damage!{Colors.RESET}\n")
            # Notify room of the dramatic spell
            broadcast_room(room, f"{Colors.CYAN}Lightning arcs wildly around the room as {caster.name} unleashes {self.name}!{Colors.RESET}\n", exclude=caster)
        else:
            send_to_player(caster, f"{Colors.YELLOW}Your {self.name} hits all enemies in the room for {damage} damage!{Colors.RESET}\n")
        
        # Report individual hits
        for target in hit_targets:
            target_name = target.short_desc if hasattr(target, 'short_desc') else target.name
            send_to_player(caster, f"  {target_name} takes {damage} damage!\n")
        
        # Report defeats and collect cleanup tasks
        mobs_to_remove = []
        players_to_handle = []
        
        for target in defeated_targets:
            target_name = target.short_desc if hasattr(target, 'short_desc') else target.name
            send_to_player(caster, f"{Colors.GREEN}{target_name} has been defeated by your spell!{Colors.RESET}\n")
            
            # Collect mobs to remove (don't modify list while iterating)
            if target in room.mobs:
                mobs_to_remove.append(target)
            
            # Collect players to handle death
            if hasattr(target, 'connection_handler'):
                players_to_handle.append(target)
        
        # Now safely remove defeated mobs from room
        for mob in mobs_to_remove:
            room.mobs.remove(mob)
        
        # Handle player deaths
        for target in players_to_handle:
            target.hp = target.max_hp
            target.mana = target.max_mana
            safe_room_vnum = 2201
            if safe_room_vnum in rooms:
                # Remove from current room's player list
                if hasattr(room, 'players') and target in room.players:
                    room.players.remove(target)
                # Move to safe room
                target.current_room = rooms[safe_room_vnum]
                if not hasattr(target.current_room, 'players'):
                    target.current_room.players = []
                target.current_room.players.append(target)
                send_to_player(target, "You have been resurrected in the starting room.\n")
        
        return True

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


def cast_spell(player, spell_name, target_name=None):
    """Cast a spell from the player's spellbook"""
    # Check if player knows the spell
    if spell_name not in player.spellbook:
        send_to_player(player, f"You don't know the spell '{spell_name}'.\n")
        return
    
    spell = player.spellbook[spell_name]
    
    # Check if player has enough mana
    if player.mana < spell.mana_cost:
        send_to_player(player, f"You don't have enough mana to cast {spell.name}. (Need {spell.mana_cost}, have {player.mana})\n")
        return
    
    # Find target if needed
    target = None
    if spell.requires_target:
        if not target_name:
            send_to_player(player, f"{spell.name} requires a target. Usage: cast {spell_name} <target>\n")
            return
            
        # Look for target in current room
        for mob in player.current_room.mobs:
            if target_name.lower() in [k.lower() for k in mob.keywords]:
                target = mob
                break
        
        # If not found in mobs, check players
        if not target:
            for p in player.current_room.players:
                if p != player and p.name.lower() == target_name.lower():
                    target = p
                    break
        
        if not target:
            send_to_player(player, f"There is no '{target_name}' here to target.\n")
            return
    
    # Consume mana
    player.mana -= spell.mana_cost
    
    # Cast the spell using the new system
    try:
        success = spell.cast(player, target)
        if success:
            # Notify room of spell casting
            spell_msg = f"{player.name} casts {spell.name}!"
            if target:
                spell_msg += f" targeting {target.short_desc if hasattr(target, 'short_desc') else target.name}!"
            broadcast_room(player.current_room, f"{Colors.MAGENTA}{spell_msg}{Colors.RESET}\n", exclude=player)
        else:
            # Refund mana if spell failed
            player.mana += spell.mana_cost
    except Exception as e:
        # Refund mana on error
        player.mana += spell.mana_cost
        send_to_player(player, f"The spell fizzles and fails!\n")
        print(f"Error casting spell {spell_name}: {e}")
        import traceback
        traceback.print_exc()

def learn_spell(player, spell_name):
    """Allow player to learn a new spell"""
    # Check if spell exists
    if spell_name not in spells:
        send_to_player(player, f"There is no spell called '{spell_name}'.\n")
        send_to_player(player, "Available spells: " + ", ".join(spells.keys()) + "\n")
        return
    
    # Check if player already knows the spell
    if spell_name in player.spellbook:
        send_to_player(player, f"You already know {spells[spell_name].name}.\n")
        return
    
    # Add the spell to player's spellbook
    spell = spells[spell_name]
    player.spellbook[spell_name] = spell
    
    # Send success message
    send_to_player(player, f"{Colors.CYAN}You have learned {spell.name}!{Colors.RESET}\n")
    send_to_player(player, f"Description: {spell.description}\n")
    send_to_player(player, f"Mana Cost: {spell.mana_cost}\n")
    
    # Notify the room
    broadcast_room(player.current_room, f"{Colors.CYAN}{player.name} learns a new spell!{Colors.RESET}\n", exclude=player)

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
    #print(f"DEBUG: Entering parse_mob with idx={idx}")

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

    # Skip blank lines
    while idx < len(lines) and lines[idx].strip() == '':
        idx += 1

    # Act flags, affect flags, alignment, etc. line (just skip for now)
    if idx < len(lines) and not lines[idx].startswith('#'):
        act_line = lines[idx].strip()
        idx += 1

    # Skip blank lines
    while idx < len(lines) and lines[idx].strip() == '':
        idx += 1

    # Level and dice line, e.g.:
    # "55 0 -10 35d35+1000 4d9+15"
    # Format: level thac0 ac hp_dice mana_dice damage_dice
    level = 1
    ac = 0
    hp = 50   # temporary defaults
    damage = 5
    if idx < len(lines) and not lines[idx].startswith('#') and lines[idx].strip():
        stat_line = lines[idx].strip()
        idx += 1
        parts = stat_line.split()
        if len(parts) >= 6:
            try:
                level = int(parts[0])
            except ValueError:
                print(f"Warning: Invalid level '{parts[0]}' in area file line {idx + 1}, using default level 1")
                level = 1
            # parts[1] = thac0 (unused here)
            try:
                ac = int(parts[2])
            except ValueError:
                print(f"Warning: Invalid AC '{parts[2]}' in area file line {idx + 1}, using default AC 0")
                ac = 0
            hp_dice = parts[3]
            # parts[4] = mana_dice (unused)
            damage_dice = parts[5]

            # We'll parse dice, but we won't rely on them for HP since the next line gives raw HP.
            import re
            dice_pattern = re.compile(r'(\d+)d(\d+)(?:\+(\d+))?')

            def parse_dice(dice_str):
                match = dice_pattern.match(dice_str)
                if match:
                    num = int(match.group(1))
                    size = int(match.group(2))
                    bonus = int(match.group(3)) if match.group(3) else 0
                    # Average value
                    return int(num * (size + 1) / 2) + bonus
                return 1

            # We'll store these in case we need fallback
            hp_from_dice = parse_dice(hp_dice)
            damage_from_dice = parse_dice(damage_dice)
            # Assign them for now; we'll override HP with the next line
            hp = hp_from_dice
            damage = damage_from_dice

    # Next line often contains gold and xp, e.g.:
    # "50000 3500000" which might represent HP and XP in your custom format
    # According to your snippet, Tiamat's HP is supposed to be 50000 here.
    if idx < len(lines) and not lines[idx].startswith('#') and lines[idx].strip():
        gold_xp_line = lines[idx].strip()
        idx += 1
        gparts = gold_xp_line.split()
        # If following DIKU conventions, this line is gold and xp.
        # But here we assume the first number is actually raw HP you want to use.
        # Let's treat the first number as HP and second as XP (or just ignore XP for now).
        if gparts and gparts[0] != '~':
            try:
                raw_hp = int(gparts[0])
                hp = raw_hp  # Override hp from dice with the raw HP
            except ValueError:
                print(f"Warning: Invalid HP value '{gparts[0]}' in area file line {idx}, keeping default HP {hp}")
        # xp = int(gparts[1]) # If you need XP

    # Position line
    if idx < len(lines) and not lines[idx].startswith('#') and lines[idx].strip():
        pos_line = lines[idx].strip()
        idx += 1
        # Not used here, but can parse if needed

    # Create the mob with parsed stats - area file mobs are hostile, not NPCs
    mob = Mobile(vnum, keywords.split(), short_desc, long_desc, description, level, is_npc=False)
    # Override defaults with parsed stats
    mob.hp = hp
    mob.max_hp = hp
    mob.defense = ac
    mob.attack_power = damage
    
    #print(f"DEBUG: Parsed mob '{mob.short_desc}' (VNUM: {vnum}) with HP: {mob.hp}, Level: {mob.level}, Defense: {mob.defense}, Attack Power: {mob.attack_power}")


    mobiles[vnum] = mob
    return idx

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

    # At this point, we have read the basic mob info. 
    # Next lines contain mob stats in DIKU/MERC format.
    # We will read lines until we encounter a '#' (start of next mob or end section).

    # First expected line: act/affect/alignment flags or similar. We'll skip detailed parsing.
    while idx < len(lines) and lines[idx].strip() == '':
        idx += 1
    if idx >= len(lines) or lines[idx].startswith('#'):
        # No additional lines found, fallback to defaults
        mobiles[vnum] = Mobile(vnum, keywords.split(), short_desc, long_desc, description, 1, is_npc=False)
        return idx

    act_line = lines[idx].strip()
    idx += 1

    # Next line often contains: level hitroll AC hp_dice mana_dice damage_dice
    # Example: "55 0 -10 35d35+1000 4d9+15"
    if idx >= len(lines) or lines[idx].startswith('#') or lines[idx].strip() == '':
        # If missing, fallback
        mobiles[vnum] = Mobile(vnum, keywords.split(), short_desc, long_desc, description, 1, is_npc=False)
        return idx

    stat_line = lines[idx].strip()
    idx += 1
    parts = stat_line.split()
    # Expected: level hitroll AC HPdice ManaDice DamDice
    # We'll focus on level, AC, HPdice, DamDice here
    level = int(parts[0])
    # hitroll = parts[1], but we won't use directly here
    ac = int(parts[2])
    hp_dice = parts[3]
    # mana_dice = parts[4] (not used in this code)
    damage_dice = parts[5]

    # Parse HP dice (format: XdY+Z)
    import re
    dice_pattern = re.compile(r'(\d+)d(\d+)(?:\+(\d+))?')

    def parse_dice(dice_str):
        match = dice_pattern.match(dice_str)
        if match:
            num = int(match.group(1))
            size = int(match.group(2))
            bonus = int(match.group(3)) if match.group(3) else 0
            # We'll use the average roll as mob HP/damage baseline
            # Average of one die is (size+1)/2, so average total = num * (size+1)/2 + bonus
            return int(num * (size + 1) / 2) + bonus
        return 1  # fallback if parsing fails

    hp = parse_dice(hp_dice)
    damage = parse_dice(damage_dice)

    # Next line typically: gold xp
    # Example: "50000 3500000"
    if idx < len(lines) and not lines[idx].startswith('#') and lines[idx].strip():
        gold_xp_line = lines[idx].strip()
        idx += 1
        gparts = gold_xp_line.split()
        # gold = int(gparts[0])  # if needed
        # xp = int(gparts[1])    # if needed
    else:
        # If not present, just continue
        pass

    # Next line might be position, default position, sex or other stats
    # Example: "9 9 2"
    # We'll just move past it if present
    if idx < len(lines) and not lines[idx].startswith('#') and lines[idx].strip():
        pos_line = lines[idx].strip()
        idx += 1
        # pparts = pos_line.split()
        # Could parse position, sex, etc. if needed.

    # Create the mob with parsed stats - area file mobs are hostile, not NPCs
    mob = Mobile(vnum, keywords.split(), short_desc, long_desc, description, level, is_npc=False)
    # Override default stats with parsed stats
    mob.hp = hp
    mob.max_hp = hp
    # Use AC as a form of defense
    # If you'd like AC to directly translate differently, adjust here
    mob.defense = ac
    mob.attack_power = damage

    mobiles[vnum] = mob
    return idx

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
    mobiles[vnum] = Mobile(vnum, keywords.split(), short_desc, long_desc,
                           description, level, is_npc=False)
    return idx

def parse_object(lines, idx):
    # In a full DIKU/MERC/ROM parser you'd extract object info here.
    # For now, we just skip ahead.
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
    idx += 1  # skip room flags line
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
            # Secret codes will be set from area file SECRET_CODE entries
            secret_code = None
            
            exit_data = {
                'description': exit_description,
                'keywords': exit_keywords,
                'door_flags': door_flags,
                'key_vnum': key_vnum,
                'to_room_vnum': to_room_vnum,
                'is_open': door_flags in (0, 2),
                'is_locked': door_flags in (2, 3),
                'secret_code': secret_code
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
                mob.current_room = rooms[room_vnum]
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

    # Example manual resets:
    goblin_rooms = [2203, 2204]
    for room_vnum in goblin_rooms:
        if room_vnum in rooms and 2300 in mobiles:
            goblin_template = mobiles[2300]
            goblin = copy.deepcopy(goblin_template)
            goblin.current_room = rooms[room_vnum]
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
        spell = Spell(
            name=spell_data['name'],
            description=spell_data['description'],
            mana_cost=spell_data['mana_cost'],
            spell_type=spell_data['spell_type'],
            requires_target=spell_data.get('requires_target', False),
            damage_multiplier=spell_data.get('damage_multiplier', 0),
            base_damage=spell_data.get('base_damage', [0, 0]),
            heal_multiplier=spell_data.get('heal_multiplier', 0),
            base_heal=spell_data.get('base_heal', [0, 0])
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
        
        # Place NPC in designated room
        if npc.room_vnum and npc.room_vnum in rooms:
            npc.current_room = rooms[npc.room_vnum]
            npc.current_room.mobs.append(npc)
        
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
                for it_data in quest_rewards['items']:
                    obj_keywords = it_data['keywords']
                    if isinstance(obj_keywords, str):
                        obj_keywords = obj_keywords.split()
                    it = {
                        'vnum': it_data['vnum'],
                        'keywords': obj_keywords,
                        'short_desc': it_data['short_desc'],
                        'long_desc': it_data['long_desc'],
                        'description': it_data['description'],
                        'item_type': it_data.get('item_type', 'misc'),
                        'effects': it_data.get('effects', {})
                    }
                    rewards['items'].append(it)
            quest = Quest(
                name=quest_data['name'],
                description=quest_data['description'],
                objectives=objectives,
                rewards=rewards
            )
            npc.quest = quest

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
        print(f"DEBUG WEB SEND: WebConnectionHandler.send_message called, connected={self.is_connected()}")
        if self.is_connected():
            try:
                # Strip ANSI color codes for web display
                clean_message = re.sub(r'\x1b\[[0-9;]*m', '', message)
                clean_message = clean_message.rstrip('\n')
                
                print(f"DEBUG WEB SEND: Emitting to session {self.session_id}: {repr(clean_message[:50])}")
                self.socketio.emit('message', {
                    'content': clean_message,
                    'type': 'game'
                }, room=self.session_id)
            except Exception as e:
                print(f"Error sending message to web client {self.session_id}: {e}")
                self.connected = False
        else:
            print(f"DEBUG WEB SEND: Not connected, skipping message")
    
    def receive_line(self):
        """Receive line from web client (synchronous simulation)"""
        # For web interface, this is handled differently - commands come via websocket
        # Return empty string to indicate web interface doesn't use synchronous input
        return ""
    
    def close_connection(self):
        """Close web connection"""
        self.connected = False
        if self.socketio:
            try:
                self.socketio.emit('disconnect', room=self.session_id)
            except:
                pass
    
    def is_connected(self):
        """Check if web connection is active"""
        return self.connected
    
    def handle_command_input(self, command):
        """Handle command input from web interface"""
        self.pending_input.put(command)

class Player:
    def __init__(self, name, current_room_vnum, connection_handler):
        self.name = name
        if current_room_vnum not in rooms:
            print(f"Warning: Room {current_room_vnum} not found, using default room 2201")
            current_room_vnum = 2201
        self.current_room = rooms.get(current_room_vnum)
        if not self.current_room:
            raise ValueError(f"No valid room found for player {name}")
        
        # Add player to room's player list
        if not hasattr(self.current_room, 'players'):
            self.current_room.players = []
        self.current_room.players.append(self)
        
        # Use connection handler instead of direct socket
        self.connection_handler = connection_handler
        
        # Keep client_socket for backward compatibility with existing code
        self.client_socket = getattr(connection_handler, 'client_socket', None)
        self.strength = 5
        self.agility = 5
        self.intelligence = 5
        self.vitality = 8
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
        return self.strength * 3

    def calculate_defense(self):
        return int(self.agility * 1.5)

    def calculate_max_hp(self):
        return self.vitality * 15

    def calculate_max_mana(self):
        return self.intelligence * 15

    def describe_current_room(self):
        send_to_player(self, f"\n{Colors.BOLD}{Colors.CYAN}{self.current_room.name}{Colors.RESET}\n")
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
            send_to_player(self, f"{Colors.YELLOW}Exits: {', '.join(exits)}{Colors.RESET}\n")
        else:
            send_to_player(self, f"{Colors.YELLOW}No obvious exits.{Colors.RESET}\n")

        for mob in self.current_room.mobs:
            send_to_player(self, f"{Colors.RED}You see {mob.short_desc} here.{Colors.RESET}\n")
        # Show other players
        for p_name, p in players.items():
            if p != self and p.current_room == self.current_room:
                send_to_player(self, f"{Colors.GREEN}You see {p.name} here.{Colors.RESET}\n")
        for obj in self.current_room.objects:
            send_to_player(self, f"{Colors.MAGENTA}You see {obj.short_desc} here.{Colors.RESET}\n")
            
        # Show active events in this room
        if self.current_room.vnum in active_events:
            event = active_events[self.current_room.vnum]
            if event['type'] == 'portal':
                portal_desc = f"{Colors.MAGENTA}⚡ A {event['data']['color']} portal swirls mysteriously here! ⚡{Colors.RESET}\n"
                send_to_player(self, portal_desc)
            elif event['type'] == 'merchant':
                merchant_desc = f"{Colors.YELLOW}🚚 {event['data']['name']} has set up shop here with exotic wares! 🚚{Colors.RESET}\n"
                send_to_player(self, merchant_desc)
            elif event['type'] == 'invasion':
                invasion_desc = f"{Colors.RED}🗡️  This area is under attack by {event['data']['invasion_name']}! 🗡️{Colors.RESET}\n"
                send_to_player(self, invasion_desc)
        
        if self.companion:
            send_to_player(self, f"{Colors.GREEN}Your companion {self.companion.name} is here.{Colors.RESET}\n")
        if self.current_pet:
            send_to_player(self, f"{Colors.GREEN}Your pet {self.current_pet.name} is here.{Colors.RESET}\n")

    def pick_up(self, obj):
        self.inventory.append(obj)
        send_to_player(self, f"You picked up {obj.short_desc}.\n")

    def show_inventory(self):
        send_to_player(self, f"{Colors.BOLD}{Colors.MAGENTA}Inventory:{Colors.RESET}\n")
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
        send_to_player(self, f"{Colors.BOLD}{Colors.BLUE}Your Skills:{Colors.RESET}\n")
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
                # Remove player from old room
                old_room = self.current_room
                if hasattr(old_room, 'players') and self in old_room.players:
                    old_room.players.remove(self)
                
                # Move to new room
                self.current_room = rooms[next_room_vnum]
                
                # Add player to new room
                if not hasattr(self.current_room, 'players'):
                    self.current_room.players = []
                self.current_room.players.append(self)
                
                send_to_player(self, f"\nYou move {Colors.YELLOW}{direction}{Colors.RESET} to {Colors.CYAN}{self.current_room.name}{Colors.RESET}.\n")
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
        send_to_player(self, f"\n{Colors.BOLD}{Colors.BLUE}Player Stats:{Colors.RESET}\n")
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
                send_to_player(self, f"You teleport to {Colors.CYAN}{self.current_room.name}{Colors.RESET}.\n")
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
                send_to_player(self, f"You teleport to {Colors.CYAN}{self.current_room.name}{Colors.RESET}.\n")
                self.describe_current_room()
                if self.companion:
                    self.companion.current_room = self.current_room
                if self.current_pet:
                    self.current_pet.current_room = self.current_room
                self.rooms_visited.add(self.current_room.vnum)
                return
        send_to_player(self, "No room with that name exists.\n")

    def show_map(self):
        send_to_player(self, f"{Colors.BOLD}{Colors.BLUE}Map:{Colors.RESET}\n")
        for dir_num, exit_data in self.current_room.exits.items():
            direction = direction_map[dir_num]
            to_room_vnum = exit_data['to_room_vnum']
            adjacent_room = rooms.get(to_room_vnum)
            if adjacent_room:
                send_to_player(self, f"{direction.capitalize()}: {adjacent_room.name}\n")
            else:
                send_to_player(self, f"{direction.capitalize()}: Unknown area\n")


    def view_achievements(self):
        send_to_player(self, f"{Colors.BOLD}{Colors.BLUE}Achievements:{Colors.RESET}\n")
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
                mob.current_room.mobs.remove(mob)
                send_to_player(self, f"You have successfully tamed {mob.short_desc} as your pet!\n")
                unlock_achievement('Pet Tamer', self)
            else:
                send_to_player(self, "Your taming attempt failed!\n")
        else:
            send_to_player(self, "You can't tame that creature.\n")

    def view_pets(self):
        send_to_player(self, f"{Colors.BOLD}{Colors.BLUE}Your Pets:{Colors.RESET}\n")
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

    def to_dict(self):
        return {
            'name': self.name,
            'strength': self.strength,
            'agility': self.agility,
            'intelligence': self.intelligence,
            'vitality': self.vitality,
            'skill_points': self.skill_points,
            'max_hp': self.max_hp,
            'hp': self.hp,
            'max_mana': self.max_mana,
            'mana': self.mana,
            'attack_power': self.attack_power,
            'defense': self.defense,
            'level': self.level,
            'experience': self.experience,
            'inventory': [obj.to_dict() if hasattr(obj, 'to_dict') else {'vnum': obj.vnum, 'keywords': obj.keywords, 'short_desc': obj.short_desc, 'long_desc': obj.long_desc, 'description': obj.description, 'item_type': obj.item_type, 'effects': obj.effects} for obj in self.inventory],
            'equipment': {slot: (obj.to_dict() if hasattr(obj, 'to_dict') else {'vnum': obj.vnum, 'keywords': obj.keywords, 'short_desc': obj.short_desc, 'long_desc': obj.long_desc, 'description': obj.description, 'item_type': obj.item_type, 'effects': obj.effects} if obj else None) for slot, obj in self.equipment.items()},
            'status_effects': self.status_effects,
            'spells': list(self.spellbook.keys()),
            'quests': [{'name': q.name, 'description': q.description, 'objectives': q.objectives, 'rewards': q.rewards, 'is_completed': q.is_completed} if hasattr(q, 'name') else q for q in self.quests],
            'reputation': self.reputation,
            'karma': self.karma,
            'achievements': [(a.name, a.description, a.is_unlocked) for a in self.achievements],
            'pets': [(p.name, p.level, p.hp, p.max_hp) for p in self.pets],
            'current_pet': self.current_pet.name if self.current_pet else None,
            'rooms_visited': list(self.rooms_visited),
            'current_room_vnum': self.current_room.vnum if self.current_room else 2201
        }

    def from_dict(self, data):
        self.strength = data['strength']
        self.agility = data['agility']
        self.intelligence = data['intelligence']
        self.vitality = data['vitality']
        self.skill_points = data['skill_points']
        self.max_hp = data['max_hp']
        self.hp = data['hp']
        self.max_mana = data['max_mana']
        self.mana = data['mana']
        self.attack_power = data['attack_power']
        self.defense = data['defense']
        self.level = data['level']
        self.experience = data['experience']
        # Reconstruct inventory objects
        self.inventory = []
        if 'inventory' in data:
            for obj_data in data['inventory']:
                if isinstance(obj_data, dict) and all(key in obj_data for key in ['vnum', 'keywords', 'short_desc', 'long_desc', 'description', 'item_type', 'effects']):
                    obj = Object(obj_data['vnum'], obj_data['keywords'], obj_data['short_desc'], 
                               obj_data['long_desc'], obj_data['description'], obj_data['item_type'], 
                               obj_data['effects'])
                    self.inventory.append(obj)
        
        # Reconstruct equipment objects  
        self.equipment = {'weapon': None, 'armor': None, 'ring': None, 'amulet': None}
        if 'equipment' in data and isinstance(data['equipment'], dict):
            for slot, obj_data in data['equipment'].items():
                if obj_data is None:
                    self.equipment[slot] = None
                elif isinstance(obj_data, dict) and all(key in obj_data for key in ['vnum', 'keywords', 'short_desc', 'long_desc', 'description', 'item_type', 'effects']):
                    obj = Object(obj_data['vnum'], obj_data['keywords'], obj_data['short_desc'],
                               obj_data['long_desc'], obj_data['description'], obj_data['item_type'],
                               obj_data['effects'])
                    self.equipment[slot] = obj
                else:
                    self.equipment[slot] = None
        self.status_effects = data['status_effects']
        # Reconstruct quest objects
        self.quests = []
        if 'quests' in data:
            for quest_data in data['quests']:
                if isinstance(quest_data, dict) and all(key in quest_data for key in ['name', 'description', 'objectives', 'rewards']):
                    quest = Quest(quest_data['name'], quest_data['description'], quest_data['objectives'], quest_data['rewards'])
                    if 'is_completed' in quest_data:
                        quest.is_completed = quest_data['is_completed']
                    self.quests.append(quest)
        self.reputation = data['reputation']
        self.karma = data['karma']
        self.achievements = [Achievement(n, d, u) for n, d, u in data['achievements']]
        self.spellbook = {}
        for s_name in data['spells']:
            if s_name in spells:
                self.spellbook[s_name] = spells[s_name]
        self.pets = []
        for p_data in data['pets']:
            p_name, p_level, p_hp, p_max_hp = p_data
            pet = Pet(p_name, None)
            pet.level = p_level
            pet.hp = p_hp
            pet.max_hp = p_max_hp
            self.pets.append(pet)
        self.current_pet = None
        if data['current_pet']:
            for pet in self.pets:
                if pet.name == data['current_pet']:
                    self.current_pet = pet
                    break
        self.rooms_visited = set(data['rooms_visited'])
        crv = data['current_room_vnum']
        if crv in rooms:
            self.current_room = rooms[crv]
        else:
            self.current_room = rooms[2201]

class Pet:
    def __init__(self, name, current_room):
        self.name = name
        self.current_room = current_room
        self.max_hp = 30
        self.hp = self.max_hp
        self.attack_power = 20
        self.defense = 4
        self.level = 2
        self.experience = 0

    def attack(self, target):
        # Use the same combat system as players for consistency
        player_attack(self, target)

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

    def attack(self, target):
        # Use the same combat system as players for consistency  
        player_attack(self, target)

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
    trader.current_room = room
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
    mob.current_room = room
    room.mobs.append(mob)

def unlock_achievement(name, player):
    achievement = achievements.get(name)
    if achievement and not achievement.is_unlocked:
        achievement.is_unlocked = True
        player.achievements.append(achievement)
        send_to_player(player, f"{Colors.GREEN}Achievement Unlocked: {achievement.name}!{Colors.RESET}\n")

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

def send_to_player(entity, message):
    # Only send if entity is a Player and has a connection handler
    if isinstance(entity, Player) and hasattr(entity, 'connection_handler'):
        try:
            entity.connection_handler.send_message(message)
        except (BrokenPipeError, OSError):
            # Player disconnected unexpectedly
            # Save their profile and remove them from the game
            with players_lock:
                if entity.name in players:
                    save_player_profile(entity)
                    # Remove from room players list
                    if hasattr(entity, 'current_room') and hasattr(entity.current_room, 'players'):
                        if entity in entity.current_room.players:
                            entity.current_room.players.remove(entity)
                    del players[entity.name]

def broadcast_room(room, message, exclude=None):
    with players_lock:
        players_list = list(players.items())
    for p_name, p in players_list:
        if p.current_room == room and p is not exclude:
            send_to_player(p, message)

def broadcast_all(message):
    with players_lock:
        players_list = list(players.items())
    for p_name, p in players_list:
        send_to_player(p, message)

def find_mob_in_room(room, mob_name):
    mob_name = mob_name.lower()
    for mob in room.mobs:
        if mob_name in [kw.lower() for kw in mob.keywords]:
            return mob
    return None

def find_target_in_room_by_index(room, target_name, index=1):
    target_name = target_name.lower()
    matches = []
    
    # Check all players (both telnet and web are now in the same dictionary)
    for p_name, pl in players.items():
        if pl.current_room == room and target_name in pl.name.lower():
            matches.append(pl)
    
    # Check mobs
    for mob in room.mobs:
        # Check short_desc first
        if target_name in mob.short_desc.lower():
            matches.append(mob)
        else:
            # Check keywords
            for kw in mob.keywords:
                if target_name in kw.lower():
                    matches.append(mob)
                    break
    
    if index <= len(matches) and index > 0:
        return matches[index-1]
    return None

def get_target_name(entity):
    # Handle both telnet Players and WebPlayers by checking for name attribute
    if hasattr(entity, 'name') and not getattr(entity, 'is_npc', False):
        return entity.name
    else:
        return entity.short_desc

def player_attack(attacker, defender):
    # Calculate base stats
    attack_power = attacker.attack_power
    defense = defender.defense
    attacker_level = getattr(attacker, 'level', 1)
    defender_level = getattr(defender, 'level', 1)
    
    # Roll for hit/miss (based on level difference and stats)
    hit_chance = 85 + (attacker_level - defender_level) * 5
    hit_chance = max(10, min(95, hit_chance))  # Clamp between 10-95%
    
    if random.randint(1, 100) > hit_chance:
        # Miss
        miss_messages = [
            f"You swing wildly but miss {get_target_name(defender)}!",
            f"Your attack goes wide of {get_target_name(defender)}!",
            f"{get_target_name(defender)} dodges your attack!",
            f"You lose your footing and miss {get_target_name(defender)}!"
        ]
        send_to_player(attacker, f"{Colors.YELLOW}{random.choice(miss_messages)}{Colors.RESET}\n")
        if isinstance(defender, Player):
            send_to_player(defender, f"{Colors.YELLOW}{get_target_name(attacker)}'s attack misses you!{Colors.RESET}\n")
        broadcast_room(attacker.current_room, f"{Colors.YELLOW}{get_target_name(attacker)} misses {get_target_name(defender)}!{Colors.RESET}\n", 
                      exclude=[attacker, defender] if isinstance(defender, Player) else [attacker])
        return
    
    # Calculate damage with more variety
    base_damage = random.randint(attack_power // 2, attack_power + attack_power // 4)
    
    # Check for critical hit (5% base chance, +1% per level advantage)
    crit_chance = 5 + max(0, attacker_level - defender_level)
    is_critical = random.randint(1, 100) <= crit_chance
    
    if is_critical:
        base_damage = int(base_damage * 1.5)
    
    # Apply defense
    damage = max(1, base_damage - defense)
    
    # Apply damage with variance based on constitution/toughness
    damage_variance = random.randint(-damage//4, damage//4) if damage > 4 else 0
    final_damage = max(1, damage + damage_variance)
    
    # Apply damage with bounds checking
    defender.hp = max(0, defender.hp - final_damage)
    
    # Create varied combat messages
    if is_critical:
        crit_messages = [
            f"You land a devastating blow on {get_target_name(defender)}!",
            f"You strike {get_target_name(defender)} with incredible force!",
            f"A perfect strike hits {get_target_name(defender)}!",
            f"You find a weak spot in {get_target_name(defender)}'s defense!"
        ]
        send_to_player(attacker, f"{Colors.RED}{random.choice(crit_messages)} ({final_damage} damage){Colors.RESET}\n")
        if isinstance(defender, Player):
            send_to_player(defender, f"{Colors.RED}{get_target_name(attacker)} critically hits you for {final_damage} damage!{Colors.RESET}\n")
        broadcast_room(attacker.current_room, f"{Colors.RED}{get_target_name(attacker)} lands a critical hit on {get_target_name(defender)}!{Colors.RESET}\n",
                      exclude=[attacker, defender] if isinstance(defender, Player) else [attacker])
    else:
        # Normal hit messages
        hit_messages = [
            f"You strike {get_target_name(defender)} solidly!",
            f"Your weapon finds its mark on {get_target_name(defender)}!",
            f"You connect with a solid blow to {get_target_name(defender)}!",
            f"You land a hit on {get_target_name(defender)}!"
        ]
        send_to_player(attacker, f"{Colors.GREEN}{random.choice(hit_messages)} ({final_damage} damage){Colors.RESET}\n")
        if isinstance(defender, Player):
            send_to_player(defender, f"{Colors.RED}{get_target_name(attacker)} attacks you for {final_damage} damage!{Colors.RESET}\n")
    
    if isinstance(defender, Player):
        send_to_player(defender, f"Your HP: {defender.hp}/{defender.max_hp}\n")
    
    if defender.hp <= 0:
        handle_defeat(attacker, defender)

def perform_special_attack(attacker, defender):
    """Enhanced special attack with different abilities based on class/level"""
    attacker_level = getattr(attacker, 'level', 1)
    
    # Different special attacks based on level
    special_types = []
    if attacker_level >= 1:
        special_types.extend(['power_strike', 'whirlwind'])
    if attacker_level >= 5:
        special_types.extend(['stunning_blow', 'precise_strike'])  
    if attacker_level >= 10:
        special_types.extend(['devastating_attack', 'multi_strike'])
        
    if not special_types:
        special_types = ['power_strike']
        
    special_type = random.choice(special_types)
    
    if special_type == 'power_strike':
        # Double damage attack
        attack_power = attacker.attack_power * 2
        damage = max(1, random.randint(attack_power//2, attack_power) - defender.defense)
        defender.hp -= damage
        send_to_player(attacker, f"{Colors.RED}You channel your energy into a devastating power strike! ({damage} damage){Colors.RESET}\n")
        if isinstance(defender, Player):
            send_to_player(defender, f"{Colors.RED}{get_target_name(attacker)} unleashes a power strike on you for {damage} damage!{Colors.RESET}\n")
        broadcast_room(attacker.current_room, f"{Colors.RED}{get_target_name(attacker)} performs a devastating power strike on {get_target_name(defender)}!{Colors.RESET}\n",
                      exclude=[attacker, defender] if isinstance(defender, Player) else [attacker])
                      
    elif special_type == 'whirlwind':
        # Attack all enemies in room  
        targets = [m for m in attacker.current_room.mobs if not m.is_npc and m != attacker]
        other_players = [p for p_name, p in players.items() if p != attacker and p.current_room == attacker.current_room]
        all_targets = targets + other_players
        
        if len(all_targets) > 1:
            send_to_player(attacker, f"{Colors.CYAN}You spin in a deadly whirlwind, striking all enemies!{Colors.RESET}\n")
            for target in all_targets:
                damage = max(1, random.randint(attacker.attack_power//3, attacker.attack_power//2) - target.defense)
                target.hp -= damage
                send_to_player(attacker, f"{Colors.CYAN}Your whirlwind hits {get_target_name(target)} for {damage} damage!{Colors.RESET}\n")
                if isinstance(target, Player):
                    send_to_player(target, f"{Colors.RED}You're caught in {get_target_name(attacker)}'s whirlwind for {damage} damage!{Colors.RESET}\n")
        else:
            # Fall back to power strike if only one target
            damage = max(1, random.randint(attacker.attack_power, attacker.attack_power * 2) - defender.defense)
            defender.hp -= damage
            send_to_player(attacker, f"{Colors.CYAN}You perform a spinning strike! ({damage} damage){Colors.RESET}\n")
            
    elif special_type == 'stunning_blow':
        # Normal damage but might stun (not implemented yet, just flavor)
        damage = max(1, random.randint(attacker.attack_power, attacker.attack_power + 5) - defender.defense)
        defender.hp -= damage
        send_to_player(attacker, f"{Colors.MAGENTA}You deliver a stunning blow that leaves {get_target_name(defender)} reeling! ({damage} damage){Colors.RESET}\n")
        if isinstance(defender, Player):
            send_to_player(defender, f"{Colors.RED}{get_target_name(attacker)}'s stunning blow hits you for {damage} damage!{Colors.RESET}\n")
            
    elif special_type == 'precise_strike':
        # High accuracy, guaranteed crit
        damage = max(1, int((random.randint(attacker.attack_power, attacker.attack_power + 3) - defender.defense) * 1.5))
        defender.hp -= damage
        send_to_player(attacker, f"{Colors.YELLOW}You carefully aim and deliver a precise strike! ({damage} damage){Colors.RESET}\n")
        if isinstance(defender, Player):
            send_to_player(defender, f"{Colors.RED}{get_target_name(attacker)}'s precise strike finds a weak point for {damage} damage!{Colors.RESET}\n")
            
    elif special_type == 'devastating_attack':
        # Triple damage but lower accuracy
        if random.randint(1, 100) <= 70:  # 70% hit chance
            damage = max(1, random.randint(attacker.attack_power * 2, attacker.attack_power * 3) - defender.defense)
            defender.hp -= damage
            send_to_player(attacker, f"{Colors.RED}You unleash a devastating attack! ({damage} damage){Colors.RESET}\n")
        else:
            send_to_player(attacker, f"{Colors.YELLOW}Your devastating attack misses as you overcommit!{Colors.RESET}\n")
            return
            
    elif special_type == 'multi_strike':
        # Multiple smaller attacks
        total_damage = 0
        hits = random.randint(2, 4)
        for i in range(hits):
            damage = max(1, random.randint(attacker.attack_power//3, attacker.attack_power//2) - defender.defense//2)
            defender.hp -= damage
            total_damage += damage
        send_to_player(attacker, f"{Colors.CYAN}You unleash a flurry of {hits} strikes! (Total: {total_damage} damage){Colors.RESET}\n")
        if isinstance(defender, Player):
            send_to_player(defender, f"{Colors.RED}{get_target_name(attacker)} hits you with a flurry of {hits} attacks for {total_damage} total damage!{Colors.RESET}\n")
    
    if isinstance(defender, Player):
        send_to_player(defender, f"Your HP: {defender.hp}/{defender.max_hp}\n")
    
    if defender.hp <= 0:
        handle_defeat(attacker, defender)
    elif hasattr(defender, 'hp') and defender.hp > 0 and isinstance(defender, Mobile) and not defender.is_npc:
        # Mob retaliates
        player_attack(defender, attacker)

def handle_defeat(attacker, defender):
    # Only stop combat with this specific defender
    stop_combat(attacker, defender)
    
    if isinstance(defender, Player):
        msg = f"{Colors.GREEN}{get_target_name(defender)} has been defeated by {get_target_name(attacker)}!{Colors.RESET}\n"
        broadcast_room(defender.current_room, msg)
        defender.hp = defender.max_hp
        defender.mana = defender.max_mana
        safe_room_vnum = 2201
        if safe_room_vnum in rooms:
            defender.current_room = rooms[safe_room_vnum]
            send_to_player(defender, "You have been resurrected in the starting room.\n")
            defender.describe_current_room()
    else:
        # Defender is a mob
        if isinstance(attacker, Player):
            send_to_player(attacker, f"{Colors.GREEN}You have defeated {defender.short_desc}!{Colors.RESET}\n")
            
            # Check if this is an invasion monster for bonus rewards
            base_xp = defender.level * 20
            if hasattr(defender, 'vnum') and 9900 <= defender.vnum <= 9999:  # Invasion monster
                # Check if we're in an invasion event
                if attacker.current_room.vnum in active_events and active_events[attacker.current_room.vnum]['type'] == 'invasion':
                    multiplier = active_events[attacker.current_room.vnum]['data']['reward_multiplier']
                    bonus_xp = int(base_xp * (multiplier - 1))
                    attacker.experience += base_xp + bonus_xp
                    send_to_player(attacker, f"{Colors.YELLOW}Invasion Bonus: +{bonus_xp} extra experience!{Colors.RESET}\n")
                else:
                    attacker.experience += base_xp
            else:
                attacker.experience += base_xp
                
            unlock_achievement('First Blood', attacker)
            if defender.current_room and defender in defender.current_room.mobs:
                defender.current_room.mobs.remove(defender)
            check_level_up(attacker)
            
            # Check if there are other hostile mobs in the room to continue combat with
            remaining_hostile_mobs = [m for m in attacker.current_room.mobs if not m.is_npc and m.hp > 0]
            if remaining_hostile_mobs and not in_combat(attacker):
                # Start combat with the next available mob
                next_target = remaining_hostile_mobs[0]
                start_combat(attacker, next_target)
                send_to_player(attacker, f"{Colors.YELLOW}{next_target.short_desc} continues the fight!{Colors.RESET}\n")

def check_level_up(player):
    required_xp = player.level * 100
    if player.experience >= required_xp:
        player.level += 1
        player.skill_points += 5
        send_to_player(player, f"{Colors.GREEN}You have leveled up to level {player.level}!{Colors.RESET}\n")
        send_to_player(player, "You have gained 5 skill points to allocate.\n")
        player.max_hp = player.calculate_max_hp()
        player.hp = player.max_hp
        player.max_mana = player.calculate_max_mana()
        player.mana = player.max_mana
        player.attack_power = player.calculate_attack_power()
        player.defense = player.calculate_defense()
        if player.level == 10:
            unlock_achievement('Level 10', player)

def llm_chat(conversation_history):
    llm_config = config['llm']
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
    llm_url = f"http://{llm_config['server_ip']}:{llm_config['server_port']}/v1/chat/completions"
    try:
        response = requests.post(llm_url, json=data, headers=headers)
        response.raise_for_status()
        result = response.json()
        ai_reply = result.get('choices', [{}])[0].get('message', {}).get('content', '').strip()
        if not ai_reply:
            ai_reply = "I'm sorry, I don't have anything to say right now."
        return ai_reply
    except (requests.RequestException, requests.ConnectionError, requests.Timeout, ValueError, KeyError) as e:
        print(f"AI service error: {e}")
        return "I'm sorry, I don't have anything to say right now."

def receive_line(player):
    if hasattr(player, 'connection_handler'):
        return player.connection_handler.receive_line()
    else:
        # Fallback for backward compatibility
        try:
            if hasattr(player, 'client_socket') and player.client_socket:
                data = player.client_socket.recv(1024)
                if not data:
                    return None  # Connection lost
                return data.decode('utf-8', errors='ignore').strip()
        except (socket.timeout, socket.error, OSError, UnicodeDecodeError) as e:
            print(f"Socket error receiving from {player.name}: {e}")
            return None  # Connection lost

def talk_to_npc(player, npc_name):
    npc = None
    for mob in player.current_room.mobs:
        if mob.is_npc and npc_name.lower() in [k.lower() for k in mob.keywords]:
            npc = mob
            break

    if not npc:
        send_to_player(player, "There is no one here by that name to talk to.\n")
        return

    room_vnum = player.current_room.vnum
    
    # Get all NPCs in the room for group conversation
    room_npcs = [mob for mob in player.current_room.mobs if mob.is_npc]

    # Check if a conversation is active in this room
    if room_vnum not in chat_sessions:
        conversation_history = npc.conversation_history.copy()
        if not conversation_history:
            # Create system prompts for all NPCs in the room
            if len(room_npcs) == 1:
                npc_context = npc.personality if npc.personality else npc.description
                npc_context = npc_context[:500]
                system_prompt = f"You are {npc.short_desc}, an NPC in a text-based RPG. Background: {npc_context}"
            else:
                # Multi-NPC conversation setup
                npc_descriptions = []
                for room_npc in room_npcs:
                    context = room_npc.personality if room_npc.personality else room_npc.description
                    npc_descriptions.append(f"{room_npc.short_desc}: {context[:200]}")
                
                system_prompt = f"You are participating in a group conversation in a text-based RPG. The NPCs present are: {', '.join([n.short_desc for n in room_npcs])}. Character backgrounds: {' | '.join(npc_descriptions)}. Respond as {npc.short_desc} but acknowledge others may also respond."
            
            conversation_history.append({"role": "system", "content": system_prompt})

        chat_sessions[room_vnum] = {
            'npc': npc,
            'npcs': room_npcs,  # Store all NPCs in the conversation
            'conversation': conversation_history
        }

        if len(room_npcs) == 1:
            broadcast_room(player.current_room, f"{player.name} starts talking to {npc.short_desc}.\n", exclude=player)
            send_to_player(player, f"You start a conversation with {npc.short_desc}.\n")
        else:
            npc_names = ', '.join([n.short_desc for n in room_npcs])
            broadcast_room(player.current_room, f"{player.name} starts a group conversation with {npc_names}.\n", exclude=player)
            send_to_player(player, f"You start a group conversation with {npc_names}.\n")
        
        send_to_player(player, "Others can join by using 'talk <npc>'. Use 'say <message>' to continue chatting.\n")
        
        # Generate initial NPC greeting
        initial_greeting = f"Hello there! I'm {npc.short_desc}. What would you like to talk about?"
        ai_reply = llm_chat(conversation_history + [{"role": "user", "content": "A player approaches you to start a conversation. Greet them naturally and ask how you can help."}])
        
        # Broadcast NPC's initial response
        broadcast_room(player.current_room, f"{Colors.BLUE}{npc.short_desc}: {ai_reply}{Colors.RESET}\n", exclude=None)
        
        # Add the greeting exchange to history
        chat_sessions[room_vnum]['conversation'].append({"role": "user", "content": "Hello"})
        chat_sessions[room_vnum]['conversation'].append({"role": "assistant", "content": ai_reply})

    else:
        chat_data = chat_sessions[room_vnum]
        if 'conversation' not in chat_data:
            npc_context = npc.personality if npc.personality else npc.description
            npc_context = npc_context[:500]
            system_prompt = f"You are {npc.short_desc}, an NPC in a text-based RPG. Background: {npc_context}"
            conversation_history = [{"role": "system", "content": system_prompt}]
            chat_data['conversation'] = conversation_history

        send_to_player(player, f"You join the ongoing conversation.\n")
        broadcast_room(player.current_room, f"{player.name} joins the conversation.\n", exclude=player)

def show_help(player):
    send_to_player(player, f"\n{Colors.BOLD}{Colors.CYAN}Available Commands:{Colors.RESET}\n")
    send_to_player(player, "Movement: north, south, east, west, up, down\n")
    send_to_player(player, "attack <target> [<number>], special, rest, stand, inventory, stats, skills, allocate <skill> <points>\n")
    send_to_player(player, "get <item>, look, map, teleport <room>, craft <item1> <item2>, quests, achievements\n")
    send_to_player(player, "spells, cast <spell> [target], learn [spell] - Magic system\n")
    send_to_player(player, "talk <npc>, say <message>, stop, open <direction>, close <direction>, unlock <direction>\n")
    send_to_player(player, "shout <message>, who, summon <mob_name>, equip <item_name>\n")
    send_to_player(player, "help, quit\n")

def show_quests(player):
    send_to_player(player, f"{Colors.BOLD}{Colors.BLUE}Active Quests:{Colors.RESET}\n")
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

def save_player_profile(player):
    data = player.to_dict()
    try:
        with open(f'player_saves/{player.name}.json', 'w') as f:
            json.dump(data, f, indent=2)
    except (IOError, OSError) as e:
        print(f"Error saving player {player.name}: {e}")

def load_player_profile(player):
    path = f'player_saves/{player.name}.json'
    if os.path.exists(path):
        try:
            with open(path, 'r') as f:
                data = json.load(f)
            player.from_dict(data)
        except (IOError, OSError, json.JSONDecodeError) as e:
            print(f"Error loading player {player.name}: {e}")
            # Remove corrupted JSON file
            if isinstance(e, json.JSONDecodeError):
                print(f"Removing corrupted JSON save file for {player.name}")
                try:
                    os.remove(path)
                except:
                    pass
            
            # Try to load legacy pickle file as fallback
            pickle_path = f'player_saves/{player.name}.pkl'
            if os.path.exists(pickle_path):
                try:
                    with open(pickle_path, 'rb') as f:
                        data = pickle.load(f)
                    player.from_dict(data)
                    # Save as JSON and remove pickle file
                    save_player_profile(player)
                    os.remove(pickle_path)
                    print(f"Migrated {player.name} from pickle to JSON")
                except Exception as migration_error:
                    print(f"Failed to migrate {player.name}: {migration_error}")
            else:
                print(f"Starting {player.name} with fresh profile (corrupted save removed)")

def open_door(player, direction):
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

def unlock_door_with_code(player, direction, code):
    """Unlock door with provided code (for web interface)"""
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
                send_to_player(player, "This door cannot be unlocked with a code.\n")
        else:
            send_to_player(player, "The door is not locked.\n")
    else:
        send_to_player(player, "There is no door in that direction.\n")

def unlock_door(player, direction):
    dir_num = reverse_direction_map.get(direction)
    if dir_num is not None and dir_num in player.current_room.exits:
        exit_data = player.current_room.exits[dir_num]
        if exit_data.get('is_locked', False):
            if 'secret_code' in exit_data and exit_data['secret_code']:
                # Check if this is a web player (can't do synchronous input)
                if hasattr(player.connection_handler, 'session_id'):
                    # Web player - tell them to use the format with code
                    send_to_player(player, "This door requires a secret code. Use: unlock <direction> <code>\n")
                else:
                    # Telnet player - prompt for input synchronously
                    send_to_player(player, "Enter the secret code to unlock the door: ")
                    code = receive_line(player).strip()
                    if code == exit_data['secret_code']:
                        exit_data['is_locked'] = False
                        send_to_player(player, "You have unlocked the door.\n")
                    else:
                        send_to_player(player, "Incorrect code. The door remains locked.\n")
            else:
                send_to_player(player, "This door cannot be unlocked with a code.\n")
        else:
            send_to_player(player, "The door is not locked.\n")
    else:
        send_to_player(player, "There is no door in that direction.\n")

def who_command(player):
    send_to_player(player, f"{Colors.BOLD}{Colors.BLUE}Players Online:{Colors.RESET}\n")
    for p_name, p in players.items():
        send_to_player(player, f"- {p_name}\n")

combatants = {}

# Dynamic World Events System
active_events = {}  # Key: room_vnum, Value: {'type': 'portal/merchant/invasion', 'data': {}, 'end_time': timestamp}
portal_connections = {}  # Key: room_vnum, Value: destination_room_vnum

def start_combat(attacker, defender):
    attacker_name = get_target_name(attacker)
    defender_name = get_target_name(defender)
    pair = tuple(sorted([attacker_name, defender_name]))
    combatants[pair] = True
    print(f"DEBUG COMBAT: Started combat - attacker: '{attacker_name}' (type: {type(attacker)}), defender: '{defender_name}' (type: {type(defender)})")
    print(f"DEBUG COMBAT: Combat pair created: {pair}, total combatants: {len(combatants)}")

def stop_combat(attacker, defender):
    pair = tuple(sorted([get_target_name(attacker), get_target_name(defender)]))
    if pair in combatants:
        del combatants[pair]

def in_combat(entity):
    # Handle both telnet Players and WebPlayers by checking for name attribute
    n = entity.name if hasattr(entity, 'name') and not getattr(entity, 'is_npc', False) else entity.short_desc
    for pair in combatants:
        if n in pair:
            return True
    return False

def find_combat_opponent(entity):
    # Handle both telnet Players and WebPlayers by checking for name attribute
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
    name_lower = name.lower()
    
    # Check telnet players
    for p_name, p in players.items():
        if p.current_room == room and p.name.lower() == name_lower:
            return p
    
    # Check web players
    global web_players_registry
    if 'web_players_registry' in globals():
        for web_player in web_players_registry.values():
            if web_player.current_room == room and web_player.name.lower() == name_lower:
                return web_player
    
    # Check mobs
    for mob in room.mobs:
        if mob.short_desc.lower() == name_lower or any(name_lower in kw.lower() for kw in mob.keywords):
            return mob
    return None

def find_entity_globally(name):
    name_lower = name.lower()
    
    # Check all players (both telnet and web are now in the same dictionary)
    for p_name, p in players.items():
        if p.name.lower() == name_lower:
            return p
    
    # Check all rooms for a matching mob
    for room in rooms.values():
        for mob in room.mobs:
            if mob.short_desc.lower() == name_lower or any(name_lower in kw.lower() for kw in mob.keywords):
                return mob
    
    return None

def combat_round():
    to_remove = []
    if combatants:  # Only print if there are active combats
        print(f"DEBUG COMBAT: Processing {len(combatants)} active combats: {list(combatants.keys())}")
    
    for pair in list(combatants.keys()):
        name1, name2 = pair
        print(f"DEBUG COMBAT: Looking for entities '{name1}' and '{name2}'")
        ent1 = find_entity_globally(name1)
        ent2 = find_entity_globally(name2)
        print(f"DEBUG COMBAT: Found ent1={ent1} (type: {type(ent1)}), ent2={ent2} (type: {type(ent2)})")

        if ent1 is None or ent2 is None:
            print(f"DEBUG COMBAT: Removing combat pair {pair} - entity not found")
            to_remove.append(pair)
            continue
        if ent1.hp <= 0 or ent2.hp <= 0:
            print(f"DEBUG COMBAT: Entity has 0 HP - ent1.hp={ent1.hp}, ent2.hp={ent2.hp}")
            to_remove.append(pair)
            continue

        print(f"DEBUG COMBAT: Executing attack - {name1} attacks {name2}")
        player_attack(ent1, ent2)
        if ent2.hp <= 0:
            print(f"DEBUG COMBAT: {name2} defeated, removing from combat")
            to_remove.append(pair)
            continue

        print(f"DEBUG COMBAT: Executing retaliation - {name2} attacks {name1}")
        player_attack(ent2, ent1)
        if ent1.hp <= 0:
            to_remove.append(pair)
            continue

    for pair in to_remove:
        if pair in combatants:
            del combatants[pair]

def shutdown_game():
    global server_running
    print("Shutting down the game server...")

    # Notify players about shutdown and close connections
    with players_lock:
        players_list = list(players.items())
    for player_name, player in players_list:
        try:
            send_to_player(player, "The server is shutting down. Your progress has been saved.\n")
            save_player_profile(player)
            # Use connection handler to close connection properly
            if hasattr(player, 'connection_handler'):
                player.connection_handler.close_connection()
            elif hasattr(player, 'client_socket'):
                player.client_socket.close()
        except Exception as e:
            print(f"Error saving or disconnecting player {player_name}: {e}")

    players.clear()
    server_running = False
    print("Game server has shut down successfully.")

def combat_loop():
    while True:
        time.sleep(2)
        combat_round()

combat_thread = threading.Thread(target=combat_loop, daemon=True)
combat_thread.start()

def process_player_command(player, command):
    """
    Shared command processing function for both telnet and web interfaces
    Returns True if player should be disconnected, False otherwise
    """
    import random  # Needed for special attack damage calculation
    global chat_sessions, players, rooms, mobiles, objects, command_abbreviations
    
    # Store original command for logging
    original_command = command
    
    room_vnum = player.current_room.vnum if hasattr(player, 'current_room') and player.current_room else None
    
    # Handle interjections in NPC chat
    if room_vnum and room_vnum in chat_sessions:
        chat_data = chat_sessions[room_vnum]
        npc = chat_data['npc']
        room_npcs = chat_data.get('npcs', [npc])  # Get all NPCs or fallback to single NPC
        
        # Route the command as a chat message if "say" is used
        if command.startswith("say "):
            message = command[4:].strip()
            if not message:
                send_to_player(player, "What do you want to say?\n")
                return False
            
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
                import random
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
                            "content": f"You are {responding_npc.short_desc} in a group conversation. Background: {npc_context[:200]}. Respond naturally as this character would, keeping responses brief since others may also respond."
                        }
                    
                    ai_reply = llm_chat(npc_specific_history)
                    broadcast_room(player.current_room, f"{Colors.BLUE}{responding_npc.short_desc}: {ai_reply}{Colors.RESET}\n", exclude=None)
                    conversation_history.append({"role": "assistant", "content": f"[{responding_npc.short_desc}] {ai_reply}"})
            
            # Update conversation history  
            chat_data['conversation'] = conversation_history
            
            # Remind player how to continue the conversation
            send_to_player(player, f"{Colors.YELLOW}[Use 'say <message>' to continue talking]{Colors.RESET}\n")
            return False
    
    # Process other commands
    if command in command_abbreviations:
        command = command_abbreviations[command]
    else:
        parts = command.split()
        if parts and parts[0] in command_abbreviations:
            parts[0] = command_abbreviations[parts[0]]
            command = ' '.join(parts)
    
    # Movement commands
    if command in ['north', 'south', 'east', 'west', 'up', 'down']:
        player.move(command)
    elif command in ['portal', 'enter portal']:
        room_vnum = player.current_room.vnum if hasattr(player, 'current_room') and player.current_room else None
        if room_vnum and room_vnum in portal_connections:
            dest_room_vnum = portal_connections[room_vnum]
            if dest_room_vnum in rooms:
                # Move through portal
                old_room = player.current_room
                if hasattr(old_room, 'players') and player in old_room.players:
                    old_room.players.remove(player)
                send_to_player(player, f"{Colors.MAGENTA}⚡ You step through the swirling portal! ⚡{Colors.RESET}\n")
                broadcast_room(old_room, f"{Colors.MAGENTA}⚡ {player.name} steps through the portal and vanishes! ⚡{Colors.RESET}\n", exclude=player)
                
                player.current_room = rooms[dest_room_vnum]
                if not hasattr(player.current_room, 'players'):
                    player.current_room.players = []
                player.current_room.players.append(player)
                
                send_to_player(player, f"{Colors.MAGENTA}⚡ You emerge from the portal in a new location! ⚡{Colors.RESET}\n")
                broadcast_room(player.current_room, f"{Colors.MAGENTA}⚡ {player.name} emerges from a swirling portal! ⚡{Colors.RESET}\n", exclude=player)
                player.describe_current_room()
            else:
                send_to_player(player, "The portal seems to lead nowhere safe. You hesitate to enter.\n")
        else:
            send_to_player(player, "There is no portal here to enter.\n")
    elif command.startswith('attack'):
        parts = command.split()
        if len(parts) >= 2:
            name = parts[1]
            index = 1
            if len(parts) == 3:
                try:
                    index = int(parts[2])
                except ValueError:
                    index = 1
            target = find_target_in_room_by_index(player.current_room, name, index)
            if target is None:
                send_to_player(player, "No such target.\n")
            else:
                if not in_combat(player) and not in_combat(target):
                    start_combat(player, target)
                player_attack(player, target)
                # Target retaliates immediately if still alive
                if hasattr(target, 'hp') and target.hp > 0 and isinstance(target, Mobile):
                    player_attack(target, player)
        else:
            mobs_in_room = [m for m in player.current_room.mobs if not m.is_npc]
            if mobs_in_room:
                mob = mobs_in_room[0]
                if not in_combat(player) and not in_combat(mob):
                    start_combat(player, mob)
                player_attack(player, mob)
                # Target retaliates immediately if still alive
                if hasattr(mob, 'hp') and mob.hp > 0 and isinstance(mob, Mobile):
                    player_attack(mob, player)
            else:
                send_to_player(player, "Attack who?\n")
    elif command == 'special':
        mobs = [m for m in player.current_room.mobs if not m.is_npc]
        other_players = [p for p_name,p in players.items() if p != player and p.current_room == player.current_room]
        if mobs:
            mob = mobs[0]
            # Slight damage variation
            attack_power = player.attack_power
            defense = mob.defense
            damage = max(1, random.randint(attack_power, attack_power+2) - defense)
            mob.hp -= damage
            send_to_player(player, f"{Colors.GREEN}You unleash a powerful strike dealing {damage} damage to {mob.short_desc}!{Colors.RESET}\n")
            if mob.hp <= 0:
                handle_defeat(player, mob)
            else:
                player_attack(mob, player)
        else:
            send_to_player(player, "There is no enemy to use 'special' on.\n")
    elif command == 'look':
        player.describe_current_room()
    elif command.startswith('get '):
        item_name = command[4:]
        found = False
        for obj in player.current_room.objects:
            if any(item_name.lower() in kw.lower() for kw in obj.keywords):
                player.inventory.append(obj)
                player.current_room.objects.remove(obj)
                send_to_player(player, f"You pick up {obj.short_desc}.\n")
                found = True
                break
        if not found:
            send_to_player(player, "There is no such item here.\n")
    elif command == 'inventory':
        if player.inventory:
            send_to_player(player, "You are carrying:\n")
            for item in player.inventory:
                send_to_player(player, f"  {item.short_desc}\n")
        else:
            send_to_player(player, "You are not carrying anything.\n")
    elif command == 'stats':
        player.show_stats()
    elif command == 'skills':
        player.show_skills()
    elif command == 'spells':
        if player.spellbook:
            send_to_player(player, f"{Colors.CYAN}Your Spellbook:{Colors.RESET}\n")
            for spell_name, spell in player.spellbook.items():
                send_to_player(player, f"  {spell.name} - {spell.description} (Mana: {spell.mana_cost})\n")
        else:
            send_to_player(player, "You don't know any spells.\n")
    elif command.startswith('cast '):
        spell_parts = command[5:].strip().split()
        if not spell_parts:
            send_to_player(player, "Usage: cast <spell> [target]\n")
        else:
            spell_name = spell_parts[0].lower()
            # Handle aliases
            if spell_name == "chain":
                spell_name = "chain lightning"
            target_name = spell_parts[1] if len(spell_parts) > 1 else None
            cast_spell(player, spell_name, target_name)
    elif command.startswith('learn '):
        spell_parts = command[6:].strip().split()
        if not spell_parts:
            send_to_player(player, "Usage: learn <spell>\n")
            send_to_player(player, "Available spells: " + ", ".join(spells.keys()) + "\n")
        else:
            spell_name = spell_parts[0].lower()
            # Handle aliases
            if spell_name == "chain":
                spell_name = "chain lightning"
            learn_spell(player, spell_name)
    elif command == 'learn':
        send_to_player(player, "Usage: learn <spell>\n")
        send_to_player(player, "Available spells to learn:\n")
        for spell_name, spell in spells.items():
            if spell_name not in player.spellbook:
                send_to_player(player, f"  {spell.name} - {spell.description} (Mana: {spell.mana_cost})\n")
        if all(spell_name in player.spellbook for spell_name in spells.keys()):
            send_to_player(player, "  You already know all available spells!\n")
    elif command.startswith('allocate '):
        parts = command.split()
        if len(parts) == 3:
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
        save_player_profile(player)
        send_to_player(player, "Your character profile has been saved.\n")
    elif command == 'load':
        load_player_profile(player)
        send_to_player(player, "Your character profile has been loaded.\n")
        player.describe_current_room()
    elif command == 'help':
        show_help(player)
    elif command == 'quests':
        show_quests(player)
    elif command == 'achievements':
        player.view_achievements()
    elif command.startswith('talk '):
        npc_name = command[5:]
        talk_to_npc(player, npc_name)
    elif command.startswith('craft '):
        parts = command.split()
        if len(parts) == 3:
            item1_name = parts[1]
            item2_name = parts[2]
            player.craft_item(item1_name, item2_name)
        else:
            send_to_player(player, "Usage: craft <item1> <item2>\n")
    elif command.startswith('open '):
        direction = command[5:]
        open_door(player, direction)
    elif command.startswith('close '):
        direction = command[6:]
        close_door(player, direction)
    elif command.startswith('unlock '):
        parts = command.split()
        if len(parts) >= 2:
            direction = parts[1]
            # Check if code is provided (for web interface)
            if len(parts) >= 3:
                code = parts[2]
                unlock_door_with_code(player, direction, code)
            else:
                unlock_door(player, direction)
        else:
            send_to_player(player, "Unlock what direction?\n")
    elif command.startswith('shout '):
        message = command[6:]
        broadcast_all(f"{Colors.YELLOW}{player.name} shouts: {message}{Colors.RESET}\n")
    elif command == 'who':
        who_command(player)
    elif command.startswith('trade') or command == 'merchant':
        # Merchant trading
        active_events = globals().get('active_events', {})
        if hasattr(player, 'current_room') and player.current_room and player.current_room.vnum in active_events and active_events[player.current_room.vnum]['type'] == 'merchant':
            event = active_events[player.current_room.vnum]
            merchant_data = event['data']
            send_to_player(player, f"{Colors.YELLOW}{merchant_data['name']}: {merchant_data['greeting']}{Colors.RESET}\n")
            send_to_player(player, f"{Colors.YELLOW}Available Items:{Colors.RESET}\n")
            for i, item in enumerate(merchant_data['items']):
                send_to_player(player, f"{i+1}. {Colors.CYAN}{item['name']}{Colors.RESET} - {item['price']} gold\n")
                send_to_player(player, f"    {item['desc']}\n")
            send_to_player(player, f"Use 'buy <number>' to purchase an item. You have {player.gold} gold.\n")
        else:
            send_to_player(player, "There's no merchant here to trade with.\n")
    elif command.startswith('buy '):
        # Buy from merchant
        active_events = globals().get('active_events', {})
        if hasattr(player, 'current_room') and player.current_room and player.current_room.vnum in active_events and active_events[player.current_room.vnum]['type'] == 'merchant':
            try:
                item_num = int(command[4:]) - 1
                event = active_events[player.current_room.vnum]
                merchant_data = event['data']
                
                if 0 <= item_num < len(merchant_data['items']):
                    item = merchant_data['items'][item_num]
                    if player.gold >= item['price']:
                        player.gold -= item['price']
                        
                        # Create the item object and add to inventory
                        new_item = Object(
                            vnum=8000 + item_num,
                            keywords=[item['name'].lower().replace(' ', '')],
                            short_desc=item['name'],
                            long_desc=item['desc'],
                            description=item['desc'],
                            item_type='equipment',
                            effects={}
                        )
                        
                        # Apply item stats
                        if 'power' in item:
                            new_item.effects['attack_bonus'] = item['power']
                        if 'defense' in item:
                            new_item.effects['defense_bonus'] = item['defense']
                        if 'heal' in item:
                            new_item.effects['heal'] = item['heal']
                        if 'mana' in item:
                            new_item.effects['mana'] = item['mana']
                            
                        player.inventory.append(new_item)
                        send_to_player(player, f"{Colors.GREEN}You purchase {item['name']} for {item['price']} gold!{Colors.RESET}\n")
                        
                        # Remove item from merchant
                        merchant_data['items'].pop(item_num)
                        
                        if not merchant_data['items']:
                            send_to_player(player, f"{Colors.YELLOW}{merchant_data['name']}: I'm all sold out! Time to move on.{Colors.RESET}\n")
                            # End merchant event early
                            del active_events[player.current_room.vnum]
                    else:
                        send_to_player(player, f"You don't have enough gold. You need {item['price']} gold but only have {player.gold}.\n")
                else:
                    send_to_player(player, "Invalid item number.\n")
            except ValueError:
                send_to_player(player, "Usage: buy <item number>\n")
        else:
            send_to_player(player, "There's no merchant here to buy from.\n")
    elif command == 'stop':
        # End any active chat session
        room_vnum = player.current_room.vnum if hasattr(player, 'current_room') and player.current_room else None
        if room_vnum and room_vnum in chat_sessions:
            del chat_sessions[room_vnum]
            send_to_player(player, "You end the conversation.\n")
            broadcast_room(player.current_room, f"{player.name} ends the conversation.\n", exclude=player)
        else:
            send_to_player(player, "There is no active conversation to stop.\n")
    elif command == 'quit':
        send_to_player(player, "Goodbye!\n")
        save_player_profile(player)
        # Use connection handler to close connection properly
        if hasattr(player, 'connection_handler'):
            player.connection_handler.close_connection()
        elif hasattr(player, 'client_socket'):
            player.client_socket.close()
        with players_lock:
            if player.name in players:
                # Remove from room players list
                if hasattr(player, 'current_room') and hasattr(player.current_room, 'players'):
                    if player in player.current_room.players:
                        player.current_room.players.remove(player)
                del players[player.name]
        return True
    else:
        send_to_player(player, "Unknown command. Type 'help' to see a list of available commands.\n")
    
    return False

def summon_command(player, mob_name):
    """Summon a mobile by name into the player's current room."""
    if not mob_name:
        send_to_player(player, "Summon what?\n")
        return
    target = find_entity_globally(mob_name)

    if target and isinstance(target, Mobile):
        # Mob found in the world, move it
        if target.current_room and target in target.current_room.mobs:
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
            if (mob_name_lower in mob_template.short_desc.lower()) or any(mob_name_lower in kw.lower() for kw in mob_template.keywords):
                found_template = mob_template
                break

        if found_template:
            new_mob = copy.deepcopy(found_template)
            new_mob.current_room = player.current_room
            player.current_room.mobs.append(new_mob)
            send_to_player(player, f"You chant ancient words, and {new_mob.short_desc} appears before you!\n")
            broadcast_room(player.current_room, f"{player.name} summons {new_mob.short_desc}!", exclude=player)
        else:
            send_to_player(player, "You can't seem to find that creature to summon.\n")

def equip_command(player, item_name):
    """Equip an item from inventory if it matches a known equipment slot."""
    if not item_name:
        send_to_player(player, "Equip what?\n")
        return
    # Find item in inventory
    item = None
    for it in player.inventory:
        if any(item_name.lower() in kw.lower() for kw in it.keywords):
            item = it
            break
    if not item:
        send_to_player(player, "You don't have that item.\n")
        return

    # Determine appropriate slot based on item_type
    slot = None
    if item.item_type == 'weapon':
        slot = 'weapon'
    elif item.item_type == 'armor':
        slot = 'armor'
    elif item.item_type == 'ring':
        slot = 'ring'
    elif item.item_type == 'amulet':
        slot = 'amulet'
    else:
        send_to_player(player, "You cannot equip that item.\n")
        return

    # If slot is occupied, unequip first
    if player.equipment[slot]:
        unequipped_item = player.equipment[slot]
        player.equipment[slot] = None
        player.inventory.append(unequipped_item)
        send_to_player(player, f"You remove {unequipped_item.short_desc}.\n")

    player.inventory.remove(item)
    player.equipment[slot] = item
    send_to_player(player, f"You equip {item.short_desc} on your {slot} slot.\n")

def player_login(client_socket):
    try:
        while True:
            try:
                client_socket.sendall(b"Welcome to the MUD! Enter your character name: ")
                data = client_socket.recv(1024)
                if not data:
                    return None
                name = data.decode('utf-8', errors='ignore').strip()
                if not name:
                    name = "Player" + str(random.randint(1000,9999))
                # Check name availability and create player atomically - thread-safe
                name_lower = name.lower()
                with players_lock:
                    existing_players_lower = [existing_name.lower() for existing_name in players.keys()]
                    if name_lower in existing_players_lower:
                        client_socket.sendall(b"That name is already in use. Please choose another name.\n")
                        continue
                    else:
                        start_room = 2201
                        # Create telnet connection handler
                        telnet_handler = TelnetConnectionHandler(client_socket)
                        p = Player(name, start_room, telnet_handler)
                        load_player_profile(p)
                        players[name] = p
                
                # Operations that don't need the lock
                for spell_name in player_spells:
                    if spell_name in spells and spell_name not in p.spellbook:
                        p.spellbook[spell_name] = spells[spell_name]
                send_to_player(p, f"Welcome, {p.name}! You appear in {p.current_room.name}.\n")
                p.describe_current_room()
                return p
            except Exception as e:
                print(f"Error during login for {name}: {e}")
                return None
    except Exception as e:
        print(f"Connection error during login: {e}")
        return None

def handle_client(client_socket):
    player = player_login(client_socket)
    if not player:
        try:
            client_socket.close()
        except:
            pass
        return
    
    try:
        while True:
            command = receive_line(player)
            if command is None:  # Connection lost
                break
            if command.strip() == "":  # Empty command, just ignore it
                continue
            
            should_disconnect = process_player_command(player, command.lower())
            if should_disconnect:
                break
                
    except Exception as e:
        print(f"Error handling client {player.name}: {e}")
    finally:
        with players_lock:
            if player.name in players:
                save_player_profile(player)
                # Remove from room players list
                if hasattr(player, 'current_room') and hasattr(player.current_room, 'players'):
                    if player in player.current_room.players:
                        player.current_room.players.remove(player)
                del players[player.name]
        try:
            client_socket.close()
        except:
            pass

    print("Shutting down the game server...")

    # Notify players about shutdown and close connections
    for player_name, player in list(players.items()):
        try:
            send_to_player(player, "The server is shutting down. Your progress has been saved.\n")
            save_player_profile(player)
            # Use connection handler to close connection properly
            if hasattr(player, 'connection_handler'):
                player.connection_handler.close_connection()
            elif hasattr(player, 'client_socket'):
                player.client_socket.close()
        except Exception as e:
            print(f"Error saving or disconnecting player {player_name}: {e}")

    players.clear()
    server_running = False
    print("Game server has shut down successfully.")

def create_portal_storm():
    """Create temporary portals linking distant rooms"""
    import time
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
        portal_data_reverse = {
            'destination': room1, 
            'color': portal_data['color']
        }
        
        active_events[room2] = {
            'type': 'portal',
            'data': portal_data_reverse,
            'end_time': end_time
        }
        
        portal_connections[room1] = room2
        portal_connections[room2] = room1
        created_portals.append((room1, room2))
        
        # Announce to players in both rooms
        if room1 in rooms:
            broadcast_room(rooms[room1], f"{Colors.MAGENTA}⚡ A {portal_data['color']} portal suddenly tears open in the air! ⚡{Colors.RESET}\n")
        if room2 in rooms:
            broadcast_room(rooms[room2], f"{Colors.MAGENTA}⚡ A {portal_data['color']} portal suddenly tears open in the air! ⚡{Colors.RESET}\n")
    
    if created_portals:
        print(f"Portal Storm created {len(created_portals)} portal pairs!")

def create_merchant_caravan():
    """Spawn a traveling merchant with rare items"""
    import time
    room_vnums = list(rooms.keys())
    if not room_vnums:
        return
        
    room_vnum = random.choice(room_vnums)
    
    # Don't overwrite existing events
    if room_vnum in active_events:
        return
        
    # Create merchant with random rare items
    merchant_names = ["Mysterious Trader", "Wandering Merchant", "Exotic Vendor", "Traveling Salesman", "Mystic Peddler"]
    merchant_name = random.choice(merchant_names)
    
    # Generate random rare items
    item_types = [
        {"name": "Enchanted Blade", "power": 15, "price": 500, "desc": "A blade that glows with inner fire"},
        {"name": "Crystal Shield", "defense": 12, "price": 400, "desc": "A shield made from pure crystal"},
        {"name": "Boots of Speed", "speed": 2, "price": 300, "desc": "Magical boots that quicken your step"},
        {"name": "Ring of Power", "power": 8, "price": 600, "desc": "A ring humming with mystical energy"},
        {"name": "Healing Potion", "heal": 50, "price": 100, "desc": "A potion that glows with healing light"},
        {"name": "Mana Crystal", "mana": 25, "price": 150, "desc": "A crystal that restores magical energy"}
    ]
    
    # Select 2-4 random items
    num_items = random.randint(2, 4)
    merchant_items = random.sample(item_types, min(num_items, len(item_types)))
    
    end_time = time.time() + random.randint(180, 420)  # 3-7 minutes
    
    active_events[room_vnum] = {
        'type': 'merchant',
        'data': {
            'name': merchant_name,
            'items': merchant_items,
            'greeting': f"Welcome, traveler! I have rare wares from distant lands!"
        },
        'end_time': end_time
    }
    
    if room_vnum in rooms:
        broadcast_room(rooms[room_vnum], f"{Colors.YELLOW}🚚 A {merchant_name} arrives with a wagon full of exotic goods! 🚚{Colors.RESET}\n")
    
    print(f"Merchant Caravan spawned: {merchant_name} in room {room_vnum}")

def create_monster_invasion():
    """Spawn dangerous monsters in a room temporarily"""
    import time
    room_vnums = list(rooms.keys())
    if not room_vnums:
        return
        
    room_vnum = random.choice(room_vnums)
    
    # Don't overwrite existing events  
    if room_vnum in active_events:
        return
        
    # Types of invasions
    invasion_types = [
        {"name": "Wolf Pack", "monsters": ["dire wolf", "alpha wolf"], "count": 3, "level": 5},
        {"name": "Goblin Raid", "monsters": ["goblin warrior", "goblin shaman"], "count": 4, "level": 3}, 
        {"name": "Skeleton Uprising", "monsters": ["skeleton warrior", "skeleton archer"], "count": 3, "level": 6},
        {"name": "Dragon Swarm", "monsters": ["young dragon", "dragon hatchling"], "count": 2, "level": 8},
        {"name": "Orc Warband", "monsters": ["orc berserker", "orc chieftain"], "count": 3, "level": 7}
    ]
    
    invasion = random.choice(invasion_types)
    
    # Create invasion monsters
    invasion_monsters = []
    for i in range(invasion["count"]):
        monster_name = random.choice(invasion["monsters"])
        monster = Mobile(
            vnum=9900 + i,  # Special vnum range for event monsters
            keywords=[monster_name.replace(" ", "")],
            short_desc=f"a {monster_name}",
            long_desc=f"A dangerous {monster_name} from the invasion force.",
            description=f"This {monster_name} looks aggressive and ready for battle.",
            level=invasion["level"] + random.randint(-1, 2)
        )
        monster.hp = monster.level * 15
        monster.max_hp = monster.hp
        monster.attack_power = monster.level * 3
        monster.defense = monster.level
        monster.is_npc = False  # Make them hostile
        invasion_monsters.append(monster)
    
    end_time = time.time() + random.randint(300, 600)  # 5-10 minutes
    
    active_events[room_vnum] = {
        'type': 'invasion', 
        'data': {
            'invasion_name': invasion["name"],
            'monsters': invasion_monsters,
            'reward_multiplier': 1.5
        },
        'end_time': end_time
    }
    
    # Add monsters to room
    if room_vnum in rooms:
        for monster in invasion_monsters:
            monster.current_room = rooms[room_vnum]
            rooms[room_vnum].mobs.append(monster)
        
        broadcast_room(rooms[room_vnum], f"{Colors.RED}🗡️  A {invasion['name']} has invaded this area! Beware! 🗡️{Colors.RESET}\n")
    
    print(f"Monster Invasion: {invasion['name']} in room {room_vnum}")

def cleanup_expired_events():
    """Remove expired events"""
    import time
    current_time = time.time()
    expired_rooms = []
    
    for room_vnum, event in active_events.items():
        if current_time >= event['end_time']:
            expired_rooms.append(room_vnum)
            
            # Clean up based on event type
            if event['type'] == 'portal':
                # Remove portal connections
                if room_vnum in portal_connections:
                    dest_room = portal_connections[room_vnum]
                    if dest_room in portal_connections:
                        del portal_connections[dest_room]
                    del portal_connections[room_vnum]
                    
                if room_vnum in rooms:
                    broadcast_room(rooms[room_vnum], f"{Colors.MAGENTA}⚡ The magical portal flickers and vanishes! ⚡{Colors.RESET}\n")
                    
            elif event['type'] == 'merchant':
                if room_vnum in rooms:
                    broadcast_room(rooms[room_vnum], f"{Colors.YELLOW}🚚 The {event['data']['name']} packs up and continues their journey. 🚚{Colors.RESET}\n")
                    
            elif event['type'] == 'invasion':
                # Remove invasion monsters that are still alive
                if room_vnum in rooms:
                    monsters_to_remove = []
                    for mob in rooms[room_vnum].mobs:
                        if hasattr(mob, 'vnum') and 9900 <= mob.vnum <= 9999:  # Event monster range
                            monsters_to_remove.append(mob)
                    
                    for monster in monsters_to_remove:
                        rooms[room_vnum].mobs.remove(monster)
                    
                    if monsters_to_remove:
                        broadcast_room(rooms[room_vnum], f"{Colors.RED}🗡️  The remaining {event['data']['invasion_name']} retreats! 🗡️{Colors.RESET}\n")
    
    # Remove expired events
    for room_vnum in expired_rooms:
        del active_events[room_vnum]

def trigger_random_event():
    """Randomly trigger one of the world events"""
    event_chance = random.randint(1, 100)
    
    if event_chance <= 15:  # 15% chance for portal storm
        create_portal_storm()
    elif event_chance <= 35:  # 20% chance for merchant caravan  
        create_merchant_caravan()
    elif event_chance <= 50:  # 15% chance for monster invasion
        create_monster_invasion()
    # 50% chance for no event

def world_events_loop():
    """Background thread for world events"""
    import time
    while True:
        try:
            # Clean up expired events
            cleanup_expired_events()
            
            # Maybe trigger a new event
            trigger_random_event()
            
            # Wait 2-5 minutes before next event check
            time.sleep(random.randint(120, 300))
        except Exception as e:
            print(f"World events error: {e}")
            time.sleep(60)

def run_server(host=None, port=None):
    if host is None:
        host = config['server']['host']
    if port is None:
        port = config['server']['port']
    global server_running
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.bind((host, port))
    server_socket.listen(5)
    print(f"Server running on {host}:{port}...")
    print(f"LLM service: {config['llm']['server_ip']}:{config['llm']['server_port']} using model '{config['llm']['model']}'")  

    def signal_handler(signal, frame):
        shutdown_game()
        server_socket.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)

    while server_running:
        try:
            server_socket.settimeout(1.0)
            try:
                client_socket, addr = server_socket.accept()
                print(f"Connection from {addr}")
                threading.Thread(target=handle_client, args=(client_socket,)).start()
            except socket.timeout:
                continue
        except Exception as e:
            print(f"Server error: {e}")

    server_socket.close()

def integrate_web_interface():
    """
    Initialize and start the web interface
    """
    try:
        # Import and initialize the web interface
        import integrated_web
        # Set the mud_multi module reference so web interface can access our globals
        integrated_web.set_mud_module(sys.modules[__name__])
        # Start the web interface
        web_thread = integrated_web.start_web_interface()
        if web_thread:
            print(f"Web interface available at: http://localhost:8080")
        else:
            print("Web interface not available")
    except ImportError as e:
        print(f"Web interface not available: {e}")
    except Exception as e:
        print(f"Web interface error: {e}")

player_spells = ['fireball', 'magic missile', 'heal', 'chain lightning']

if __name__ == "__main__":
    parse_area_file('area.txt')
    load_objects_from_file('objects.json')
    process_resets()
    place_random_treasures()
    load_spells_from_file('spells.json')
    load_npcs_from_file('npcs.json')

    npc_thread = threading.Thread(target=npc_movement_loop, daemon=True)
    npc_thread.start()
    
    # Start dynamic world events thread
    events_thread = threading.Thread(target=world_events_loop, daemon=True)
    events_thread.start()
    print("Dynamic world events system started!")

    # Start integrated web interface
    integrate_web_interface()

    try:
        run_server()
    except KeyboardInterrupt:
        shutdown_game()
        print("Server shutdown.")

