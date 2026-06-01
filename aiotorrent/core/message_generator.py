from struct import pack

import bitstring


class MessageGenerator:
	"""
		This class generates messages.
	"""
	@staticmethod
	def gen_handshake(info_hash):
		'''
		handshake:
			<pstrlen>	1   byte; string length of <pstr>, as a single raw byte; strlen = 19
			<pstr>		19 bytes; string identifier of the protocol; "BitTorrent protocol"
			<reserved>	8  bytes; eight (8) reserved bytes
			<info_hash>	20 bytes; info_hash
			<peer_id>	20 bytes; peer_id
		'''
		message = pack(
			">B19sQ20s20s",
			19,
			b"BitTorrent protocol",
			00000000,
			info_hash,
			b"ABCD" + b"X"*16			
		)

		return message


	@staticmethod
	def gen_bitfield(bit_array: bitstring.BitArray):

		serialized_bitfield = bit_array.tobytes()
		mlen = 1 + len(serialized_bitfield)
		mid = 5

		message = pack(f">IB{len(serialized_bitfield)}s", mlen, mid, serialized_bitfield)
		return message


	@staticmethod
	def gen_choke():
		mlen, mid = 1, 0
		message = pack(">IB", mlen, mid)
		return message


	@staticmethod
	def gen_unchoke():
		mlen, mid = 1, 1
		message = pack(">IB", mlen, mid)
		return message


	@staticmethod
	def gen_interested():
		mlen, mid = 1, 2
		message = pack(">IB", mlen, mid)
		return message


	@staticmethod
	def gen_request(index, offset, block_size= 2**14):
		mlen, mid = 13, 6
		message = pack(">IBIII", mlen, mid, index, offset, block_size)
		return message


	@staticmethod
	def gen_piece(index, offset, piece_data):
		payload_len = len(piece_data)
		mlen, mid = 9 + payload_len, 7
		message = pack(f">IBII{payload_len}s", mlen, mid, index, offset, piece_data)
		return message