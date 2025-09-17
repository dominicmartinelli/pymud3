#!/usr/bin/env python3
"""
Simple working web interface for PyMUD3
Based on the successful test page approach
"""

import threading
import secrets
from flask import Flask, render_template_string
from flask_socketio import SocketIO, emit, join_room

# This will be set by mud-multi.py when it imports this module
mud_multi = None

def set_mud_module(module):
    """Set the mud_multi module reference from the main server"""
    global mud_multi
    mud_multi = module

# Global web interface state
web_app = None
web_socketio = None
web_player_sessions = {}  # session_id -> player_name

def create_web_interface():
    """Create the simplified working web interface"""
    global web_app, web_socketio, web_player_sessions

    if mud_multi is None:
        raise RuntimeError("mud_multi module not set. Call set_mud_module() first.")

    # Make sure rooms are loaded
    if not hasattr(mud_multi, 'rooms') or not mud_multi.rooms:
        print("Rooms not loaded yet, initializing...")
        mud_multi.parse_area_file('area.txt')
        mud_multi.load_objects_from_file('objects.json')
        mud_multi.process_resets()
        mud_multi.load_spells_from_file('spells.json')
        mud_multi.load_npcs_from_file('npcs.json')

    web_app = Flask(__name__)
    web_app.secret_key = secrets.token_hex(16)
    web_socketio = SocketIO(web_app, cors_allowed_origins="*")

    # Simple HTML template based on working test page
    HTML_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>PyMUD3 - Web Interface</title>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.5/socket.io.js"></script>
    <style>
        body { font-family: 'Courier New', monospace; background: #000; color: #00ff00; margin: 0; padding: 0; }
        .main-container { display: flex; height: 100vh; }
        .game-area { flex: 1; padding: 20px; display: flex; flex-direction: column; }
        .side-panel { width: 300px; background: #111; border-left: 1px solid #00ff00; padding: 15px; overflow-y: auto; }
        #gameOutput { background: #111; border: 1px solid #00ff00; padding: 10px; flex: 1; overflow-y: auto; white-space: pre-wrap; margin-bottom: 10px; min-height: 400px; }
        .input-area { display: flex; gap: 10px; margin-bottom: 10px; }
        .input-area input { flex: 1; background: #222; color: #00ff00; border: 1px solid #00ff00; padding: 8px; font-family: inherit; }
        .input-area button { background: #444; color: #00ff00; border: 1px solid #00ff00; padding: 8px 15px; cursor: pointer; }
        .side-button { display: block; width: 100%; margin: 5px 0; padding: 10px; background: #444; color: #00ff00; border: 1px solid #00ff00; cursor: pointer; text-align: center; }
        .side-button:hover { background: #555; }
        .info-section { margin-bottom: 20px; border: 1px solid #00ff00; padding: 10px; background: #111; }
        .info-section h3 { margin: 0 0 10px 0; color: #00ff00; }
        .info-content { font-size: 12px; line-height: 1.4; }
        .login-overlay { position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.9); display: flex; align-items: center; justify-content: center; z-index: 1000; }
        .login-form { background: #111; border: 2px solid #00ff00; padding: 30px; text-align: center; }
        .login-form input { display: block; width: 200px; margin: 10px auto; padding: 8px; background: #222; color: #00ff00; border: 1px solid #00ff00; }
        .login-form button { margin-top: 15px; padding: 10px 20px; background: #444; color: #00ff00; border: 1px solid #00ff00; cursor: pointer; }
        .hidden { display: none; }
        .stats-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 5px; font-size: 12px; }
    </style>
</head>
<body>
    <div class="login-overlay" id="loginOverlay">
        <div class="login-form">
            <h2>Enter PyMUD3</h2>
            <input type="text" id="playerName" placeholder="Enter your name" maxlength="20">
            <button onclick="login()">Connect</button>
        </div>
    </div>

    <div class="main-container">
        <div class="game-area">
            <div id="gameOutput">Welcome to PyMUD3! Enter your name to begin...</div>
            <div class="input-area">
                <input type="text" id="commandInput" placeholder="Enter command..." disabled>
                <button onclick="sendCommand()" id="sendBtn" disabled>Send</button>
            </div>
        </div>
        <div class="side-panel">
            <button class="side-button" onclick="sendCommand('stats')">Stats & Equipment</button>
            <button class="side-button" onclick="sendCommand('inventory')">Inventory</button>
            <button class="side-button" onclick="sendCommand('spells')">Spells</button>
            <button class="side-button" onclick="sendCommand('look')">Look</button>
            <button class="side-button" onclick="sendCommand('who')">Players Online</button>

            <div class="info-section">
                <h3>Quick Commands</h3>
                <div class="info-content">
                    <button class="side-button" onclick="sendCommand('north')">North</button>
                    <button class="side-button" onclick="sendCommand('south')">South</button>
                    <button class="side-button" onclick="sendCommand('east')">East</button>
                    <button class="side-button" onclick="sendCommand('west')">West</button>
                    <button class="side-button" onclick="sendCommand('up')">Up</button>
                    <button class="side-button" onclick="sendCommand('down')">Down</button>
                </div>
            </div>

            <div class="info-section">
                <h3>Game Info</h3>
                <div id="gameInfo" class="info-content">
                    Weather: Loading...<br>
                    Time: Loading...<br>
                    Location: Unknown
                </div>
            </div>
        </div>
    </div>

    <script>
        console.log('DEBUG: Script starting');
        let connected = false;
        let socket;

        // Initialize Socket.IO
        if (typeof io === 'undefined') {
            console.error('Socket.IO not loaded!');
            document.getElementById('gameOutput').textContent = 'ERROR: Socket.IO not loaded!';
        } else {
            console.log('Socket.IO loaded, connecting...');
            socket = io('http://localhost:8080');

            socket.on('connect', function() {
                console.log('Connected to server!');
            });

            socket.on('connect_error', function(error) {
                console.log('Connection error:', error);
            });

            socket.on('login_success', function(data) {
                console.log('Login successful!');
                document.getElementById('loginOverlay').classList.add('hidden');
                document.getElementById('commandInput').disabled = false;
                document.getElementById('sendBtn').disabled = false;
                document.getElementById('commandInput').focus();
                connected = true;
            });

            socket.on('error', function(data) {
                console.log('Error:', data);
                alert('Error: ' + data.message);
            });

            socket.on('message', function(data) {
                console.log('Received message:', data);
                const output = document.getElementById('gameOutput');
                if (output) {
                    output.textContent += data.content + '\\n';
                    output.scrollTop = output.scrollHeight;

                    // Update stats and info panels
                    updateGameInfo(data.content);
                }
            });
        }

        // Function to update stats and game info from game messages
        function updateGameInfo(message) {
            // Update weather and time info
            if (message.includes('Weather:')) {
                const weatherMatch = message.match(/Weather: (\\w+)/);
                if (weatherMatch) {
                    updateGameInfoText('Weather: ' + weatherMatch[1]);
                }
            }

            if (message.includes('Time:')) {
                const timeMatch = message.match(/Time: (\\w+)/);
                if (timeMatch) {
                    updateGameInfoText('Time: ' + timeMatch[1]);
                }
            }

            // Update location when room name appears
            if (message.includes('\\n') && !message.includes('Weather:') && !message.includes('Time:')) {
                const lines = message.split('\\n');
                for (let line of lines) {
                    if (line.trim() && !line.includes('You') && !line.includes('Welcome') && line.length > 3) {
                        updateGameInfoText('Location: ' + line.trim());
                        break;
                    }
                }
            }

        }

        function updateGameInfoText(newInfo) {
            const gameInfo = document.getElementById('gameInfo');
            if (gameInfo) {
                let content = gameInfo.innerHTML;
                const lines = content.split('<br>');

                if (newInfo.startsWith('Weather:')) {
                    lines[0] = newInfo;
                } else if (newInfo.startsWith('Time:')) {
                    lines[1] = newInfo;
                } else if (newInfo.startsWith('Location:')) {
                    lines[2] = newInfo;
                }

                gameInfo.innerHTML = lines.join('<br>');
            }
        }


        // Function definitions - always available
        function login() {
            const name = document.getElementById('playerName').value.trim();
            console.log('Login attempt with name:', name);
            if (name && socket) {
                socket.emit('login', {name: name});
            } else {
                console.error('Cannot login: socket not initialized');
            }
        }

        function sendCommand(cmd) {
            if (cmd) {
                // Command from side button
                if (connected && socket) {
                    console.log('Sending button command:', cmd);
                    socket.emit('command', {command: cmd});
                } else {
                    console.error('Cannot send command: not connected or socket not initialized');
                }
            } else {
                // Command from input field
                const input = document.getElementById('commandInput');
                if (input.value.trim() && connected && socket) {
                    console.log('Sending command:', input.value.trim());
                    socket.emit('command', {command: input.value.trim()});
                    input.value = '';
                } else {
                    console.error('Cannot send command: not connected or socket not initialized');
                }
            }
        }

        // Enter key handling
        document.addEventListener('keypress', function(e) {
            if (e.key === 'Enter') {
                if (!document.getElementById('loginOverlay').classList.contains('hidden')) {
                    login();
                } else {
                    sendCommand();
                }
            }
        });
    </script>
</body>
</html>
    '''

    @web_app.route('/')
    def index():
        return render_template_string(HTML_TEMPLATE)

    @web_socketio.on('connect')
    def handle_connect(auth):
        from flask import request
        print(f"WEB: Client connected with session ID: {request.sid}")

    @web_socketio.on('login')
    def handle_login(data):
        from flask import request
        print(f"WEB: Login attempt from session {request.sid} with data: {data}")
        player_name = data.get('name', '').strip()
        print(f"WEB: Player name extracted: '{player_name}'")

        if not player_name or len(player_name) > 20:
            print(f"DEBUG WEB: Invalid name, emitting error")
            emit('error', {'message': 'Invalid name'})
            return

        # Check name availability and create player atomically - thread-safe
        player_name_lower = player_name.lower()
        try:
            with mud_multi.players_lock:
                existing_players_lower = [name.lower() for name in mud_multi.players.keys()]
                if player_name_lower in existing_players_lower:
                    emit('error', {'message': 'Name already in use'})
                    return

                # Create web connection handler and player while holding the lock
                session_id = request.sid
                web_handler = mud_multi.WebConnectionHandler(session_id, web_socketio)

                # Create player with web connection handler
                start_room = 2201
                new_player = mud_multi.Player(player_name, start_room, web_handler)
                mud_multi.load_player_profile(new_player)

                # Add to players dictionary (already holding lock)
                mud_multi.players[player_name] = new_player

            # Operations that don't need the lock
            web_player_sessions[session_id] = player_name

            # Add default spells
            for spell_name in ['fireball', 'magic missile', 'heal', 'chain lightning']:
                if spell_name in mud_multi.spells and spell_name not in new_player.spellbook:
                    new_player.spellbook[spell_name] = mud_multi.spells[spell_name]

            # Join socket room
            join_room(session_id)

            # Send welcome message
            emit('login_success', {'name': player_name})
            mud_multi.send_to_player(new_player, f"Welcome, {new_player.name}! You appear in {new_player.current_room.name}.")
            new_player.describe_current_room()

        except Exception as e:
            print(f"Error creating web player {player_name}: {e}")
            import traceback
            traceback.print_exc()
            emit('error', {'message': 'Failed to create player'})

    @web_socketio.on('command')
    def handle_command(data):
        from flask import request
        print(f"DEBUG WEB: Command received from session {request.sid}")

        if request.sid not in web_player_sessions:
            print(f"DEBUG WEB: Session {request.sid} not found in active sessions")
            emit('error', {'message': 'Not logged in'})
            return

        player_name = web_player_sessions[request.sid]
        if player_name not in mud_multi.players:
            emit('error', {'message': 'Player not found'})
            return

        player = mud_multi.players[player_name]
        command = data.get('command', '').strip().lower()

        if command:
            try:
                # Process command using unified system
                should_disconnect = mud_multi.process_player_command(player, command)
                if should_disconnect:
                    handle_web_disconnect()
            except Exception as e:
                print(f"Error processing command '{command}' for {player_name}: {e}")
                emit('error', {'message': f'Command error: {str(e)}'})

    @web_socketio.on('disconnect')
    def handle_web_disconnect():
        from flask import request
        session_id = request.sid
        if session_id in web_player_sessions:
            player_name = web_player_sessions[session_id]
            with mud_multi.players_lock:
                if player_name in mud_multi.players:
                    player = mud_multi.players[player_name]
                    mud_multi.save_player_profile(player)
                    # Remove from room players list
                    if hasattr(player, 'current_room') and hasattr(player.current_room, 'players'):
                        if player in player.current_room.players:
                            player.current_room.players.remove(player)
                    del mud_multi.players[player_name]
            del web_player_sessions[session_id]

    return web_app, web_socketio

def start_web_interface():
    """Start the simplified web interface in a separate thread"""
    try:
        web_app, web_socketio = create_web_interface()
        if not web_app:
            return None

        def run_web_server():
            try:
                web_socketio.run(web_app, host='localhost', port=8080, debug=False, allow_unsafe_werkzeug=True)
            except Exception as e:
                print(f"Web server error: {e}")

        web_thread = threading.Thread(target=run_web_server, daemon=True)
        web_thread.start()
        return web_thread

    except Exception as e:
        print(f"Failed to start web interface: {e}")
        import traceback
        traceback.print_exc()
        return None