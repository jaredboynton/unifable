export class LruCache {
  constructor(limit) {
    this.limit = limit;
    this.map = new Map();
  }

  get(key) {
    return this.map.get(key);
  }

  set(key, val) {
    this.map.set(key, val);
    if (this.map.size > this.limit) {
      this.map.delete(this.map.keys().next().value);
    }
  }
}
