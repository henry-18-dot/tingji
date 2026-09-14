class TingjiPCM extends AudioWorkletProcessor {
  constructor() { super(); this.sum = 0; this.count = 0; this.phase = 0; this.output = new Int16Array(16000); this.at = 0; this.stopped = false; this.port.onmessage = event => { if (event.data === 'flush') { this.stopped = true; this.flush(); this.port.postMessage({flushed:true}); } }; }
  flush() { if (this.at) { const buffer = this.output.slice(0, this.at).buffer; this.port.postMessage({pcm:buffer}, [buffer]); this.at = 0; } }
  process(inputs) {
    if (this.stopped) return false;
    const channels = inputs[0]; if (!channels?.length) return true;
    for (let i = 0; i < channels[0].length; i++) {
      let value = 0; for (const channel of channels) value += channel[i] / channels.length;
      this.sum += value; this.count++; this.phase += 16000;
      if (this.phase >= sampleRate) {
        this.phase -= sampleRate;
        const v = Math.max(-1, Math.min(1, this.sum / this.count));
        this.output[this.at++] = Math.round(v * (v < 0 ? 32768 : 32767));
        this.sum = 0; this.count = 0;
        if (this.at === this.output.length) this.flush();
      }
    }
    return true;
  }
}
registerProcessor('tingji-pcm', TingjiPCM);
