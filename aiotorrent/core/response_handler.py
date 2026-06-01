import logging
from os import path
from struct import unpack
from bitstring import BitArray

from aiotorrent.core.message_generator import MessageGenerator
from aiotorrent.core.util import Block


logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class PeerResponseHandler:
	def __init__(self, artifacts, peer=None):
		self.artifacts = artifacts
		self.peer = peer

	def _read_piece_from_disk(self, global_offset: int, piece_length: int) -> bytes:
		"""
		Since pieces in multi-file torrents can span across file boundaries,
		this helper function reads a piece from disk given its global offset.
		"""
		torrent_info = self.peer.torrent_info
		base_dir = torrent_info['name']

		# if this is a multi-file torrent, just use the existing file list.
		# otherwise, initialize files as a single-item list.
		if isinstance(torrent_info['files'], list):
			files = torrent_info['files']
		else:
			files = [{'path': [torrent_info['files']], 'length': torrent_info['size']}]

		buffer = b""
		bytes_remaining = piece_length
		current_global_offset = global_offset
		current_file_start = 0

		for file in files:
			file_length: int = file['length']
			current_file_end = current_file_start + file_length

			# if the offset is before the end of this file, the piece contains bytes from here
			if current_global_offset < current_file_end:

				if isinstance(file['path'], list):
					filepath = path.join(base_dir, *file['path'])
				else:
					filepath = path.join(base_dir, file['path'])

				file_read_offset = current_global_offset - current_file_start  # offset in this file to start reading the piece
				bytes_available_in_file = file_length - file_read_offset  # max bytes in this file we can read
				bytes_to_read = min(bytes_remaining, bytes_available_in_file)

				with open(filepath, 'rb') as fp:
					fp.seek(file_read_offset)
					buffer += fp.read(bytes_to_read)

				bytes_remaining -= bytes_to_read
				current_global_offset += bytes_to_read

				if bytes_remaining <= 0:
					break

			current_file_start += file_length

		return buffer


	async def handle(self):

		if logger.isEnabledFor(logging.DEBUG):
			for key, value in self.artifacts.items():
				if isinstance(value, bytes):
					logger.debug(f"{key}: {value[:32]}")

		while self.artifacts:
			if "keep_alive" in self.artifacts: self.handle_keep_alive() 
			if "choke" in self.artifacts: await self.handle_choke()
			if "unchoke" in self.artifacts: self.handle_unchoke()
			if "interested" in self.artifacts: self.handle_interested()
			if "not_interested" in self.artifacts: self.handle_not_interested()
			if "handshake" in self.artifacts: await self.handle_handshake()
			# Merged have_handler into bitfield_handler
			if "have" in self.artifacts: self.handle_bitfield()
			if "bitfield" in self.artifacts: self.handle_bitfield()
			# Piece handler is special as it returns values
			if "requests" in self.artifacts: await self.handle_requests()
			if "pieces" in self.artifacts: return self.handle_piece()
			if "cancel" in self.artifacts: self.handle_cancel()


	def handle_keep_alive(self):
		logger.debug(f'Keep-Alive from {self.peer}')
		self.artifacts.pop('keep_alive')


	async def handle_choke(self):
		await self.peer.disconnect(f"Choked client!")
		self.artifacts.pop('choke')


	def handle_unchoke(self):
		self.peer.choking_me = False
		self.peer.am_interested = True
		logger.debug(f"Unchoke from {self.peer}")
		self.artifacts.pop('unchoke')


	def handle_interested(self):
		self.peer.interested_in_me = True
		logger.debug(f"{self.peer} is interested")

		if self.peer.am_choking:
			self.peer.am_choking = False
			unchoke_msg = MessageGenerator.gen_unchoke()
			self.peer.writer.write(unchoke_msg)

		self.artifacts.pop('interested')


	def handle_not_interested(self):
		self.peer.interested_in_me = False
		logger.debug(f"{self.peer} is no longer interested")

		if not self.peer.am_choking:
			self.peer.am_choking = True
			choke_msg = MessageGenerator.gen_choke()
			self.peer.writer.write(choke_msg)

		self.artifacts.pop('not_interested')


	async def handle_handshake(self):
		message = self.artifacts['handshake']
		if not message or len(message) < 68:
			# if empty or no response, peer is inactive
			# if response is less than 68, wrong response by peer
			await self.peer.disconnect("Empty/None/Wrong handshake message! ")

		pstrlen, pstr, res, info_hash, peer_id = unpack('>B19sQ20s20s', message)

		if pstrlen != 19 or pstr != b"BitTorrent protocol":
			await self.peer.disconnect("Invalid pstrlen or pstr! ")

		handshake_response = {
			"pstrlen": pstrlen,
			"pstr": pstr,
			"reserved": res,
			"info_hash": info_hash,
			"peer_id": peer_id,
		}

		self.peer.has_handshaked = True
		self.peer.handshake_response = handshake_response

		logger.debug(f"Handshake from {self.peer}")
		self.artifacts.pop('handshake')


	def handle_bitfield(self):
		# Create bitfield from bitfield message if available
		# If not available, create empty bitfield_message
		if 'bitfield' in self.artifacts:
			message = self.artifacts['bitfield']
			pieces = BitArray(message)
		else:
			num_pieces = len(self.peer.torrent_info['piece_hashmap'])
			pieces = BitArray(num_pieces)

		# Merge have requests if available
		if 'have' in self.artifacts:
			for piece_num in self.artifacts['have']:
				pieces[piece_num] = True

		# Finally set Peer pieces value to local pieces value
		self.peer.pieces = pieces
		try:
			if 'have' in self.artifacts: self.artifacts.pop('have')
			if 'bitfield' in self.artifacts: self.artifacts.pop('bitfield')
		except KeyError:
			...
		finally:
			logger.debug(f"Bitfield from {self.peer}")


	async def handle_requests(self):
		requests = self.artifacts['requests']
		for index, offset, length in requests:
			logger.debug(f"Peer {self.peer} requested piece {index}, offset {offset}, length {length}")

			if self.peer.am_choking:
				logger.debug(f"Ignoring request from {self.peer} because they are choked.")
				continue

			local_pieces = self.peer.torrent_info.get('local_pieces')
			if not local_pieces or not local_pieces[index]:
				logger.debug(f"{self.peer} requested piece {index} which we don't have.")
				continue

			piece_len = self.peer.torrent_info['piece_len']
			global_offset = index * piece_len + offset
			piece_data = self._read_piece_from_disk(global_offset, length)

			if len(piece_data) == length:
				piece_msg = MessageGenerator.gen_piece(index, offset, piece_data)
				self.peer.writer.write(piece_msg)
				await self.peer.writer.drain()
				logger.debug(f"Sent {len(piece_data)} bytes to {self.peer}")
			else:
				logger.error(f"[seeding] Failed to read piece from disk: got {len(piece_data)} instead of {length} "
							 f"bytes for piece {index} (offset {offset})")

		self.artifacts.pop('requests')


	def handle_piece(self):
		# This is the only method which returns any value
		blocks = list()
		for block_info in self.artifacts['pieces']:
			try:
				index, offset, data = block_info
				block = Block(index, offset, data)
				blocks.append(block)
			except TypeError:
				raise TypeError(f"Handler: Failed To Extract Piece sent by {self.peer}")
			
		self.artifacts.pop('pieces')
		return blocks


	def handle_cancel(self):
		cancels = self.artifacts['cancels']
		for iol_tuple in cancels:
			logger.debug(f"Peer {self.peer} canceled request for piece {iol_tuple[0]}, offset {iol_tuple[1]}")
			self.artifacts['requests'].remove(iol_tuple)

		self.artifacts.pop('cancels')
