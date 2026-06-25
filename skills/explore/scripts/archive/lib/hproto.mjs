// Zero-dependency protobuf wire primitives (encode/decode).
// Wire format: tag = (field << 3) | wireType.
//   0 varint | 1 i64 | 2 length-delimited | 5 i32
// See https://protobuf.dev/programming-guides/encoding/
// Number-safe varints up to 2^53 (avoids 32-bit bitwise truncation).

export const WIRE = { VARINT: 0, I64: 1, LEN: 2, I32: 5 };

function varintBytes(n) {
  if (n < 0) throw new RangeError(`varint cannot be negative: ${n}`);
  const out = [];
  let v = n;
  while (v > 0x7f) {
    out.push((v % 128) + 128);
    v = Math.floor(v / 128);
  }
  out.push(v);
  return out;
}

export class Writer {
  constructor() {
    this.chunks = [];
  }
  _push(buf) {
    this.chunks.push(buf);
    return this;
  }
  tag(field, wire) {
    return this._push(Buffer.from(varintBytes((field << 3) | wire)));
  }
  // uint32/uint64/int32(>=0)/bool/enum. proto3 omits default scalars (0/false/"")
  // on the wire — they decode identically to absent — so we skip them to match
  // canonical encoders byte-for-byte.
  uint(field, value) {
    if (!value) return this; // undefined/null/0
    this.tag(field, WIRE.VARINT);
    return this._push(Buffer.from(varintBytes(value)));
  }
  bool(field, value) {
    if (!value) return this; // undefined/null/false
    this.tag(field, WIRE.VARINT);
    return this._push(Buffer.from([1]));
  }
  double(field, value) {
    if (value === undefined || value === null) return this;
    this.tag(field, WIRE.I64);
    const b = Buffer.alloc(8);
    b.writeDoubleLE(value, 0);
    return this._push(b);
  }
  string(field, value) {
    if (!value) return this; // undefined/null/"" (proto3 omits empty string)
    return this.bytes(field, Buffer.from(String(value), "utf8"));
  }
  bytes(field, value) {
    if (value === undefined || value === null) return this;
    const buf = Buffer.isBuffer(value) ? value : Buffer.from(value);
    this.tag(field, WIRE.LEN);
    this._push(Buffer.from(varintBytes(buf.length)));
    return this._push(buf);
  }
  // Sub-message: accepts a Buffer (already-encoded) or a Writer.
  message(field, value) {
    if (value === undefined || value === null) return this;
    const buf = value instanceof Writer ? value.finish() : value;
    return this.bytes(field, buf);
  }
  finish() {
    return Buffer.concat(this.chunks);
  }
}

export class Reader {
  constructor(buf) {
    this.buf = Buffer.isBuffer(buf) ? buf : Buffer.from(buf);
    this.pos = 0;
    this.len = this.buf.length;
  }
  eof() {
    return this.pos >= this.len;
  }
  varint() {
    let result = 0;
    let shift = 0;
    let byte;
    do {
      byte = this.buf[this.pos++];
      result += (byte & 0x7f) * 2 ** shift;
      shift += 7;
    } while (byte & 0x80);
    return result;
  }
  tag() {
    const t = this.varint();
    return { field: Math.floor(t / 8), wire: t & 7 };
  }
  bytes() {
    const len = this.varint();
    const out = this.buf.subarray(this.pos, this.pos + len);
    this.pos += len;
    return out;
  }
  string() {
    return this.bytes().toString("utf8");
  }
  double() {
    const v = this.buf.readDoubleLE(this.pos);
    this.pos += 8;
    return v;
  }
  skip(wire) {
    if (wire === WIRE.VARINT) this.varint();
    else if (wire === WIRE.I64) this.pos += 8;
    // NB: read the length into a temp first — `this.pos += this.varint()` would
    // bind the left this.pos before varint() advances it (off-by-one).
    else if (wire === WIRE.LEN) { const n = this.varint(); this.pos += n; }
    else if (wire === WIRE.I32) this.pos += 4;
    else throw new Error(`unknown wire type ${wire}`);
  }
}

// Iterate fields of a message buffer. cb(field, wire, reader) must consume the
// value (via reader.varint/bytes/string/double) or return false to auto-skip.
export function forEachField(buf, cb) {
  const r = new Reader(buf);
  while (!r.eof()) {
    const { field, wire } = r.tag();
    const before = r.pos;
    const handled = cb(field, wire, r);
    if (handled === false || r.pos === before) {
      // not consumed by cb — skip remaining bytes for this field
      if (r.pos === before) r.skip(wire);
    }
  }
}
