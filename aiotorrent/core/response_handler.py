import logging
from struct import unpack
from bitstring import BitArray

from aiotorrent.core.util import Block


logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class PeerResponseHandler:
	def __init__(self, artifacts, peer=None):
		self.artifacts = artifacts
		self.peer = peer


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
			if "requests" in self.artifacts: return self.handle_request()
			if "pieces" in self.artifacts: return self.handle_piece()
			if "cancel" in self.artifacts: return self.handle_cancel()



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
		self.artifacts.pop('interested')


	def handle_not_interested(self):
		self.peer.interested_in_me = False
		logger.debug(f"{self.peer} is no longer interested")
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


	async def handle_request(self):
		requests = self.artifacts['requests']
		for index, begin, length in requests:
			logger.debug(f"Peer {self.peer} requested piece {index}, offset {begin}, length {length}")

		if not self.peer.am_choking:
			pass  # TODO: read the requested block from disk and queue it to send out

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
		for index, begin, length in cancels:
			logger.debug(f"Peer {self.peer} canceled request for piece {index}, offset {begin}")

		# TODO: remove block from request queue

		self.artifacts.pop('cancels')
