from src.networking.packet_handler import PacketHandler
from src.networking.packets.serverbound import Handshake, LoginStart, EncryptionResponse, ClientStatus, \
    PlayerPositionAndLook, TeleportConfirm
from src.networking.packets.clientbound import EncryptionRequest, SetCompression, SpawnEntity, ChunkData, \
    TimeUpdate, HeldItemChange, PlayerListItem, GameState, SetSlot
from src.networking.packets.clientbound import PlayerPositionAndLook as PlayerPositionAndLookClientbound

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.padding import PKCS1v15

import select


class LoginHandler(PacketHandler):
    def __init__(self, connection, mc_connection):
        super().__init__(connection)
        self.mc_connection = mc_connection

    def handle_held_item_change(self, packet):
        if packet.id != HeldItemChange.id:
            return

        self.mc_connection.game_state.acquire()

        self.mc_connection.game_state.held_item_slot = HeldItemChange().read(packet.packet_buffer).Slot

        self.mc_connection.game_state.release()

    def handle_position(self, packet):
        if packet.id != PlayerPositionAndLook.id:
            return

        pos_packet = PlayerPositionAndLook().read(packet.packet_buffer)

        self.mc_connection.game_state.acquire()

        self.mc_connection.game_state.last_yaw = pos_packet.Yaw
        self.mc_connection.game_state.last_pitch = pos_packet.Pitch

        # Replace the currently logged PlayerPositionAndLookClientbound packet
        self.mc_connection.game_state.last_pos_packet = pos_packet

        self.mc_connection.game_state.release()

    def join_world(self):
        self.mc_connection.game_state.acquire()

        # Send the player all the packets that lets them join the world
        for id_ in self.mc_connection.game_state.join_ids:
            if id_ in self.mc_connection.game_state.packet_log:
                packet = self.mc_connection.game_state.packet_log[id_]
                self.connection.send_packet_buffer_raw(packet.compressed_buffer)

        # Send them their last position/look if it exists
        if PlayerPositionAndLookClientbound.id in self.mc_connection.game_state.packet_log:
            if self.mc_connection and self.mc_connection.game_state.last_pos_packet:
                last_packet = self.mc_connection.game_state.last_pos_packet

                pos_packet = PlayerPositionAndLookClientbound( \
                    X=last_packet.X, Y=last_packet.Y, Z=last_packet.Z, \
                    Yaw=self.mc_connection.game_state.last_yaw, Pitch=self.mc_connection.game_state.last_pitch, Flags=0, \
                    TeleportID=self.mc_connection.game_state.teleport_id)
                self.mc_connection.game_state.teleport_id += 1
                self.connection.send_packet_raw(pos_packet)
            else:
                self.connection.send_packet_buffer_raw(
                    self.mc_connection.game_state.packet_log[PlayerPositionAndLookClientbound.id] \
                        .compressed_buffer)  # Send the last packet that we got

        if TimeUpdate.id in self.mc_connection.game_state.packet_log:
            self.connection.send_packet_buffer_raw(self.mc_connection.game_state.packet_log\
                                                       [TimeUpdate.id].compressed_buffer)

        # Send the player list items (to see other players)
        self.connection.send_single_packet_dict(self.mc_connection.game_state.player_list)

        # Send all loaded chunks
        print("Sending chunks", flush=True)
        self.connection.send_single_packet_dict(self.mc_connection.game_state.chunks)
        print("Done sending chunks", flush=True)

        # Send the player all the currently loaded entities
        self.connection.send_single_packet_dict(self.mc_connection.game_state.entities)

        # Player sends ClientStatus, this is important for respawning if died
        self.mc_connection.send_packet_raw(ClientStatus(ActionID=0))

        # Send their current game state
        self.connection.send_packet_raw(GameState(Reason=self.mc_connection.game_state.gs_reason,\
                                                    Value=self.mc_connection.game_state.gs_value))
        # Send their inventory
        self.connection.send_single_packet_dict(self.mc_connection.game_state.main_inventory)

        # Send their last held item
        self.connection.send_packet_raw(HeldItemChange(Slot=self.mc_connection.game_state.held_item_slot))

        self.mc_connection.game_state.release()

    def setup(self):
        print("Reading handshake", flush=True)
        Handshake().read(self.read_packet_from_stream().packet_buffer)
        print("Reading login start", flush=True)
        LoginStart().read(self.read_packet_from_stream().packet_buffer)

        # Generate a dummy (pubkey, privkey) pair
        privkey = rsa.generate_private_key(public_exponent=65537, key_size=2048, backend=default_backend())
        pubkey = privkey.public_key().public_bytes(encoding=serialization.Encoding.DER,
                                                   format=serialization.PublicFormat.SubjectPublicKeyInfo)

        print("Trying to send encryption request", flush=True)
        self.connection.send_packet_raw(
            EncryptionRequest(ServerID='', PublicKey=pubkey, VerifyToken=self.mc_connection.VerifyToken))

        print("Encryption request sent", flush=True)

        # The encryption response will be encrypted with the server's public key
        # Luckily, when this goes wrong read_packet returns None
        _ = self.read_packet_from_stream()

        if _ is None:
            print("Invalid packet!", flush=True)
            self.connection.on_disconnect()
            return False

        encryption_response = EncryptionResponse().read(_.packet_buffer)

        # Decrypt and verify the verify token
        verify_token = privkey.decrypt(encryption_response.VerifyToken, PKCS1v15())
        assert (verify_token == self.mc_connection.VerifyToken)

        # Decrypt the shared secret
        shared_secret = privkey.decrypt(encryption_response.SharedSecret, PKCS1v15())

        # Enable encryption using the shared secret
        self.connection.enable_encryption(shared_secret)

        # Enable compression and assign the threshold to the connection
        if self.mc_connection.compression_threshold >= 0:
            self.connection.send_packet_raw(SetCompression(Threshold=self.mc_connection.compression_threshold))
            self.connection.compression_threshold = self.mc_connection.compression_threshold

        self.connection.send_packet_raw(self.mc_connection.login_success)

        print("Joining world", flush=True)
        self.join_world()
        print("Finished joining world", flush=True)

        # Let the real connection know about our client
        # Now the client can start receiving forwarded data
        self.connection.upstream.start()
        self.mc_connection.set_client_upstream(self.connection.upstream)
        print("Connected to upstream", flush=True)

        return True

    def handle(self):
        while True:
            ready_to_read = select.select([self.connection.stream], [], [], self._timeout)[0]

            if ready_to_read:
                packet = self.read_packet_from_stream()

                if packet is not None:
                    if packet and packet.id != TeleportConfirm.id: # Sending these will crash us
                        self.handle_position(packet)
                        self.handle_held_item_change(packet)
                        self.mc_connection.send_packet_buffer(packet.compressed_buffer)
                else:
                    print("Client disconnected (invalid packet). Exiting thread", flush=True)
                    self.connection.on_disconnect()
                    break

