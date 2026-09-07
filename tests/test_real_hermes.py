"""Offline real Hermes discovery, wire payload, persistence and restart test."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

REPO = Path(__file__).resolve().parents[1]


def test_real_hermes_path():
    source = Path(os.environ['HERMES_AGENT_ROOT'])
    with tempfile.TemporaryDirectory(prefix='time-awareness-real-') as tmp:
        root = Path(tmp)
        home = root/'profile'
        (home/'plugins').mkdir(parents=True)
        (root/'empty').mkdir()
        (home/'plugins/time_awareness').symlink_to(REPO/'time_awareness', target_is_directory=True)
        (home/'config.yaml').write_text('''memory:
  memory_enabled: false
  user_profile_enabled: false
model:
  streaming: false
  context_length: 128000
compression:
  enabled: false
agent:
  tool_use_enforcement: false
curator:
  enabled: false
auxiliary:
  title_generation:
    enabled: false
fallback_models: []
plugins:
  enabled: [time_awareness]
  entries:
    time_awareness:
      settings:
        timezone: Europe/London
        user_name: Sam
        platforms: [discord]
''')
        env = {'PATH':'/usr/bin:/bin', 'HOME':tmp, 'HERMES_HOME':str(home),
               'PYTHONPATH':str(source)+os.pathsep+str(REPO), 'HERMES_BUNDLED_PLUGINS':str(root/'empty'),
               'PYTHONDONTWRITEBYTECODE':'1', 'PYTHONNOUSERSITE':'1', 'HF_HUB_OFFLINE':'1',
               'LANG':'C.UTF-8', 'TZ':'UTC'}
        result = subprocess.run([sys.executable, '-B', str(Path(__file__).resolve()), '--probe'],
                                cwd=root, env=env, text=True, capture_output=True, timeout=90)
        assert result.returncode == 0, result.stdout+'\n'+result.stderr
        assert 'REAL_TIME_AWARENESS_OK' in result.stdout
        print(next(line for line in result.stdout.splitlines() if line.startswith('REAL_TIME_AWARENESS_OK')))


def probe():
    from datetime import datetime
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    root = Path(os.environ['HOME'])
    home = Path(os.environ['HERMES_HOME'])
    def audit(event, args):
        if event in ('socket.connect', 'socket.getaddrinfo'):
            address = args[1] if event == 'socket.connect' else args
            host = address[0] if isinstance(address, tuple) else None
            if host not in ('127.0.0.1', 'localhost', '::1'):
                raise PermissionError('External network forbidden in integration fixture')
        if event == 'sqlite3.connect' and args[0] != ':memory:':
            if not Path(args[0]).absolute().is_relative_to(root):
                raise PermissionError('Nonfixture database')
        if event == 'open' and isinstance(args[0], (str, bytes, os.PathLike)):
            path = Path(os.fsdecode(args[0])).absolute()
            mode, flags = args[1:3]
            writing = ((isinstance(mode,str) and any(c in mode for c in 'wax+'))
                       or (isinstance(flags,int) and flags & (os.O_WRONLY|os.O_RDWR)))
            sensitive = path.name in ('.env', 'auth.json', 'SOUL.md', 'MEMORY.md', 'USER.md')
            if not path.is_relative_to(root) and path != Path('/dev/null') and (writing or sensitive):
                raise PermissionError('Nonfixture state access')
    sys.addaudithook(audit)
    from hermes_cli import env_loader
    original = env_loader.load_hermes_dotenv
    def isolated_env(*, hermes_home=None, project_env=None, load_external_secrets=True):
        return original(hermes_home=hermes_home or home, project_env=None, load_external_secrets=False)
    env_loader.load_hermes_dotenv = isolated_env
    from run_agent import AIAgent
    from hermes_state import SessionDB
    from hermes_cli.plugins import get_plugin_manager
    from hermes_cli.lifecycle import invoke_hook
    wire = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass
        def do_POST(self):
            wire.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            response = {'id':'offline-fixture', 'object':'chat.completion', 'model':'fixture',
                        'choices':[{'index':0, 'message':{'role':'assistant', 'content':'Fixture reply.'}, 'finish_reason':'stop'}],
                        'usage':{'prompt_tokens':10,'completion_tokens':3,'total_tokens':13}}
            data = json.dumps(response).encode()
            self.send_response(200)
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    db = SessionDB(home/'state.db')
    clock = [datetime.fromisoformat('2026-09-07T23:30:00+01:00').timestamp()]
    agent = None
    def make(session):
        a = AIAgent(base_url=f'http://127.0.0.1:{server.server_port}/v1', api_key='offline-fixture-only',
                    provider='custom', api_mode='chat_completions', model='fixture', platform='discord',
                    user_id='fixture-sam', session_id=session, session_db=db, max_iterations=1,
                    enabled_toolsets=[], skip_context_files=True, load_soul_identity=False,
                    skip_memory=True, skip_background_review=True, save_trajectories=False,
                    quiet_mode=True, fallback_model={}, credential_pool=None, max_tokens=128,
                    reasoning_config={'enabled':False})
        manager = get_plugin_manager()
        manager.discover_and_load()
        hooks = [cb.__self__ for cb in manager._hooks.get('pre_llm_call', [])
                 if getattr(getattr(cb,'__self__',None),'__class__',type(None)).__name__ == 'TemporalHooks']
        assert len(hooks)==1, manager._hooks
        hooks[0].clock = lambda:clock[0]
        assert hooks[0].settings.user_name == 'Sam'
        return a, hooks[0]
    def chat(text, history=None):
        r = agent.run_conversation(text, system_message='Offline fixture system prompt.', conversation_history=history)
        assert r['completed'] and not r['failed'], r
        return r
    try:
        agent,hooks = make('old-session')
        first = chat('First real user message.')
        assert '<temporal-context' not in json.dumps(wire[-1])
        # Full unload simulates a restart; durable public PluginState must survive.
        agent.close(); agent=None
        get_plugin_manager().unload()
        clock[0] += 47*60
        agent,hooks = make('new-session')
        second = chat('Second real user message.')
        current = [m for m in wire[-1]['messages'] if m['role']=='user'][-1]['content']
        assert current.startswith('Second real user message.\n\n<memory-context>')
        assert current.count('<temporal-context') == 1
        assert 'Tuesday 8 September 2026, 00:17 BST' in current
        assert '47 minutes ago' in current and 'local date changed' in current
        before = current
        state_history = db.get_messages_as_conversation('new-session')
        user = next(m for m in state_history if m['role']=='user')
        assert user['content']=='Second real user message.' and user['api_content']==before
        clock[0] += 60
        third = chat('A prompt follow-up.', state_history)
        users = [m['content'] for m in wire[-1]['messages'] if m['role']=='user']
        assert users[0]==before and users[-1]=='A prompt follow-up.'
        assert wire[0]['messages'][0] == wire[-1]['messages'][0]
        # Public state uses task-local profile resolution, not a hardcoded home.
        from hermes_constants import set_hermes_home_override, reset_hermes_home_override
        token = set_hermes_home_override(root/'another-profile')
        try:
            assert hooks.pre_llm_call(session_id='new-session', platform='discord', turn_id='other', sender_id='fixture-sam') is None
        finally:
            reset_hermes_home_override(token)
        print('REAL_TIME_AWARENESS_OK '+json.dumps({'requests':len(wire), 'restart_and_new_session':True,
              'one_tagged_suffix':True, 'visible_content_unchanged':True, 'historical_wire_byte_stable':True,
              'system_prompt_unchanged':True, 'profile_isolation':True}))
    finally:
        if agent:
            agent.close()
        db.close()
        get_plugin_manager().unload()
        server.shutdown(); server.server_close()


if __name__ == '__main__':
    probe()
