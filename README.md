# PyMUD3 - Multi-User Dungeon Game

A modern Multi-User Dungeon (MUD) server written in Python with both telnet and web interfaces. Features a JSON-based spell system, room-based exploration, combat mechanics, and real-time multiplayer interaction.

## Features

### Core Game Systems
- **Multi-threaded MUD server** supporting concurrent players
- **Dual interfaces**: Traditional telnet client and modern web browser
- **JSON-based spell system** with configurable magic effects
- **Area-of-effect spells** including Chain Lightning
- **Room-based world** with exploration and navigation
- **Combat system** with mobs and NPCs
- **Player persistence** with automatic save/load profiles
- **Real-time chat** system for player communication

### Technical Features
- **Thread-safe** player management with proper locking
- **Socket.IO integration** for real-time web communication
- **Flask web framework** with responsive HTML interface
- **Modular architecture** with separate game logic and interfaces
- **JSON configuration** for spells, objects, NPCs, and areas

## Quick Start

### Prerequisites
```bash
pip install flask flask-socketio
```

### Running the Server
```bash
python pymud-multi.py
```

This starts:
- Telnet server on port 4000
- Web interface on http://localhost:8080

### Connecting
- **Telnet**: Use any MUD client or `telnet localhost 4000`
- **Web**: Open http://localhost:8080 in your browser

## Game Commands

### Basic Commands
- `look` - Examine your surroundings
- `inventory` - Check your items
- `stats` - View your character stats
- `help` - Get command help

### Movement
- `north`, `south`, `east`, `west`, `up`, `down`
- `n`, `s`, `e`, `w`, `u`, `d` (shortcuts)

### Combat & Magic
- `cast <spell>` - Cast a spell
- `cast <spell> <target>` - Cast targeted spell
- `spells` - List known spells
- `learn <spell>` - Learn a new spell

### Communication
- `chat <message>` - Send message to all players
- `say <message>` - Speak to players in current room

## Spell System

Spells are configured in `spells.json` with the following structure:

```json
{
    "name": "Fireball",
    "description": "A blazing ball of fire that damages a single enemy.",
    "mana_cost": 20,
    "spell_type": "offensive",
    "requires_target": true,
    "damage_multiplier": 2,
    "base_damage": [5, 15]
}
```

### Available Spells
- **Fireball** - Single-target fire damage
- **Magic Missile** - Reliable magical attack
- **Heal** - Restore health points
- **Chain Lightning** - Area-of-effect electrical damage

### Spell Types
- `offensive` - Single-target damage spells
- `area_offensive` - Multi-target damage spells
- `healing` - Health restoration spells

## File Structure

```
pymud3/
├── pymud-multi.py      # Main server and game logic
├── integrated_web.py   # Web interface implementation
├── spells.json         # Spell configurations
├── objects.json        # Game objects and items
├── npcs.json          # Non-player characters
├── area.txt           # Room descriptions and layout
├── player_saves/      # Player profile storage
└── README.md          # This file
```

## Architecture

### Core Components
- **Player Class**: Manages player state, inventory, and actions
- **Room System**: Handles world geography and object placement
- **Connection Handlers**: Separate telnet and web connection management
- **Spell Engine**: JSON-driven magic system with configurable effects
- **Threading Model**: Safe concurrent access with player locks

### Web Interface
- **Flask** application serving the game interface
- **Socket.IO** for real-time bidirectional communication
- **Responsive design** with terminal-style aesthetics
- **Real-time updates** for room info, player lists, and NPCs

## Configuration

### Adding New Spells
Edit `spells.json` to add new magical effects:

```json
{
    "name": "New Spell",
    "description": "Description of spell effect",
    "mana_cost": 25,
    "spell_type": "offensive|healing|area_offensive",
    "requires_target": true|false,
    "damage_multiplier": 2,
    "base_damage": [min, max]
}
```

### Server Ports
- Telnet: Port 4000 (configurable in `pymud-multi.py`)
- Web: Port 8080 (configurable in `integrated_web.py`)

## Development

### Thread Safety
The server uses locks for thread-safe operations:
- `players_lock` protects the global players dictionary
- Player actions are processed atomically
- Room modifications are synchronized

### Adding Features
1. **New Commands**: Add to `process_player_command()` in `pymud-multi.py`
2. **New Spells**: Update `spells.json` with spell configuration
3. **New Areas**: Extend `area.txt` with room descriptions
4. **Web Features**: Modify `integrated_web.py` for browser enhancements

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Test with both telnet and web interfaces
5. Submit a pull request

## License

This project is open source. Feel free to use, modify, and distribute.

## Troubleshooting

### Common Issues
- **Port conflicts**: Change ports in source files if 4000/8080 are in use
- **Player not found**: Check thread safety if experiencing connection issues
- **Spell fizzling**: Verify spell exists in `spells.json` and player has sufficient mana
- **Web interface not loading**: Ensure Flask and Socket.IO are installed

### Debug Mode
Run with Python's debug flag for detailed error output:
```bash
python -u pymud-multi.py
