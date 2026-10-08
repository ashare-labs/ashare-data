import sys

def _deny_network(event, args):
    if event in ('socket.connect', 'socket.connect_ex', 'socket.getaddrinfo', 'socket.gethostbyname', 'socket.gethostbyaddr', 'socket.bind', 'socket.sendto'):
        raise RuntimeError('Independent review forbids network: ' + event)
sys.addaudithook(_deny_network)
