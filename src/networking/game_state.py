from threading import RLock


class GameState:
    def __init__(self, join_ids=[]):
        self.held_item_slot = 0
        self.last_pos_packet = None
        self.last_yaw = 0
        self.last_pitch = 0
        self.teleport_id = 0

        self.gs_reason = 0
        self.gs_value = 0.0

        self.abilities = None

        self.state_lock = RLock()

        # Every other packet goes here
        self.packet_log = {}

        self.main_inventory = {}
        self.chunks = {}
        self.player_list = {}
        self.entities = {}

        self.join_ids = join_ids

    def acquire(self):
        self.state_lock.acquire()

    def release(self):
        self.state_lock.release()