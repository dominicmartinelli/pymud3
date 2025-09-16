#!/usr/bin/env python3
"""
Integrated Web Interface for PyMUD3
Works with the unified Player and ConnectionHandler architecture
"""

import threading
import secrets
from flask import Flask, render_template_string, request
from flask_socketio import SocketIO, emit, join_room, leave_room

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
    """Create the integrated web interface"""
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
    
    # HTML template for the web interface
    HTML_TEMPLATE = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>PyMUD3 - Web Interface</title>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.5/socket.io.js"></script>
    <script>
        // Ensure Socket.IO loads before continuing
        if (typeof io === 'undefined') {
            document.write('<script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.0.1/socket.io.js"><\\/script>');
        }
    </script>
    <style>
        body { font-family: 'Courier New', monospace; background: #000; color: #00ff00; margin: 0; padding: 20px; }
        .container { max-width: 1200px; margin: 0 auto; display: flex; gap: 20px; height: 100vh; }
        .game-area { flex: 1; display: flex; flex-direction: column; }
        .output { flex: 1; background: #111; border: 1px solid #00ff00; padding: 10px; overflow-y: auto; white-space: pre-wrap; font-size: 14px; margin-bottom: 10px; color: #00ff00; }
        .input-area { display: flex; gap: 10px; }
        .input-area input { flex: 1; background: #222; color: #00ff00; border: 1px solid #00ff00; padding: 8px; font-family: inherit; }
        .input-area button { background: #444; color: #00ff00; border: 1px solid #00ff00; padding: 8px 15px; cursor: pointer; }
        .input-area button:hover { background: #666; }
        .sidebar { width: 300px; display: flex; flex-direction: column; gap: 10px; }
        .panel { background: #111; border: 1px solid #00ff00; padding: 10px; }
        .panel h3 { margin: 0 0 10px 0; color: #ffff00; }
        .login-overlay { position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.9); display: flex; align-items: center; justify-content: center; z-index: 1000; }
        .login-form { background: #111; border: 2px solid #00ff00; padding: 30px; text-align: center; }
        .login-form input { display: block; width: 200px; margin: 10px auto; padding: 8px; background: #222; color: #00ff00; border: 1px solid #00ff00; }
        .login-form button { margin-top: 15px; padding: 10px 20px; background: #444; color: #00ff00; border: 1px solid #00ff00; cursor: pointer; }
        .hidden { display: none; }
        .quick-btn { width: 100%; margin: 2px 0; padding: 5px; background: #333; color: #00ff00; border: 1px solid #00ff00; cursor: pointer; }
        .quick-btn:hover { background: #555; }
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

    <div class="container">
        <div class="game-area">
            <div class="output" id="gameOutput">Welcome to PyMUD3! Enter your name to begin...</div>
            <div class="input-area">
                <input type="text" id="commandInput" placeholder="Enter command..." disabled>
                <button onclick="sendCommand()" id="sendBtn" disabled>Send</button>
            </div>
        </div>
        <div class="sidebar">
            <div class="panel">
                <h3>Room Info</h3>
                <div id="roomInfo">Not connected</div>
            </div>
            <div class="panel">
                <h3>Players</h3>
                <div id="playerList">None</div>
            </div>
            <div class="panel">
                <h3>NPCs</h3>
                <div id="npcList">None</div>
            </div>
            <div class="panel">
                <h3>Quick Commands</h3>
                <button onclick="sendQuickCommand('look')" class="quick-btn">Look</button>
                <button onclick="sendQuickCommand('inventory')" class="quick-btn">Inventory</button>
                <button onclick="sendQuickCommand('stats')" class="quick-btn">Stats</button>
                <button onclick="sendQuickCommand('help')" class="quick-btn">Help</button>
            </div>
        </div>
    </div>

    <script>
        console.log('DEBUG CLIENT: Script starting');

        // Check if Socket.IO loaded
        if (typeof io === 'undefined') {
            console.error('DEBUG CLIENT: Socket.IO not loaded!');
            alert('Socket.IO failed to load. Check your internet connection.');
        } else {
            console.log('DEBUG CLIENT: Socket.IO library loaded successfully');
        }

        const socket = io();
        console.log('DEBUG CLIENT: Socket.IO initialized');
        let connected = false;

        socket.on('connect', function() {
            console.log('DEBUG CLIENT: Socket connected successfully');
        });

        socket.on('connect_error', function(error) {
            console.log('DEBUG CLIENT: Socket connection error:', error);
        });

        socket.on('disconnect', function() {
            console.log('DEBUG CLIENT: Socket disconnected');
        });

        socket.on('message', function(data) {
            console.log('DEBUG CLIENT: Received message:', data);
            const output = document.getElementById('gameOutput');
            console.log('DEBUG CLIENT: Output element found:', !!output);
            if (output) {
                console.log('DEBUG CLIENT: Adding content:', data.content);
                output.textContent += data.content + '\n';
                output.scrollTop = output.scrollHeight;
                console.log('DEBUG CLIENT: Message added successfully');
            } else {
                console.error('DEBUG CLIENT: gameOutput element not found!');
            }
        });

        socket.on('login_success', function(data) {
            document.getElementById('loginOverlay').classList.add('hidden');
            document.getElementById('commandInput').disabled = false;
            document.getElementById('sendBtn').disabled = false;
            document.getElementById('commandInput').focus();
            connected = true;
        });

        socket.on('error', function(data) {
            alert('Error: ' + data.message);
        });

        function login() {
            console.log('DEBUG CLIENT: Login function called');
            const name = document.getElementById('playerName').value.trim();
            console.log('DEBUG CLIENT: Player name:', name);
            if (name) {
                console.log('DEBUG CLIENT: Emitting login event with name:', name);
                socket.emit('login', {name: name});
            } else {
                console.log('DEBUG CLIENT: No name entered');
            }
        }

        function sendCommand() {
            const input = document.getElementById('commandInput');
            if (input.value.trim() && connected) {
                socket.emit('command', {command: input.value.trim()});
                input.value = '';
            }
        }

        function sendQuickCommand(cmd) {
            if (connected) {
                socket.emit('command', {command: cmd});
            }
        }

        document.getElementById('commandInput').addEventListener('keypress', function(e) {
            if (e.key === 'Enter') {
                sendCommand();
            }
        });

        document.getElementById('playerName').addEventListener('keypress', function(e) {
            if (e.key === 'Enter') {
                login();
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
    def handle_connect():
        print(f"DEBUG WEB: Client connected with session ID: {request.sid}")

    @web_socketio.on('login')
    def handle_login(data):
        print(f"DEBUG WEB: Login attempt from session {request.sid} with data: {data}")
        player_name = data.get('name', '').strip()
        print(f"DEBUG WEB: Player name extracted: '{player_name}'")
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
        print(f"DEBUG WEB: Command received from session {request.sid}")
        print(f"DEBUG WEB: Active sessions: {list(web_player_sessions.keys())}")

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
                import traceback
                traceback.print_exc()
                emit('error', {'message': f'Command error: {str(e)}'})
    
    @web_socketio.on('disconnect')
    def handle_web_disconnect():
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
    """Start the integrated web interface in a separate thread"""
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
        print(f"Failed to start integrated web interface: {e}")
        import traceback
        traceback.print_exc()
        return None