/* voice-worklet.js — AudioWorklet-процессоры голосового канала.
   Загружается через ctx.audioWorklet.addModule("/static/voice-worklet.js")
   из voice.js (раньше жил строкой-шаблоном в index.html и собирался в
   Blob — при разбиении клиента на модули переехал в настоящий файл).

   VcCap — захват: копит сэмплы и шлёт батчами по 20мс (960 сэмплов
   при 48кГц, v2.0).
   VcPlay — плейаут: джиттер-буфер с пре-буфером, фейды ~2мс на стыках
   звук↔тишина (без щелчков «ТЦЦЦ»), догон при переполнении. */

class VcCap extends AudioWorkletProcessor {
  constructor(){ super(); this.b = []; this.n = 0; }
  process(inputs){
    const ch = inputs[0] && inputs[0][0];
    if (ch){
      /* v3.6.7 ФИКС «БУРУНДУКА»: копируем чанк СРАЗУ (new Float32Array).
         По спеке AudioWorklet массив inputs валиден ТОЛЬКО внутри текущего
         process(): Chrome переиспользует ту же память под следующий рендер.
         Раньше мы копили ССЫЛКИ и копировали позже, при флеше батча —
         к этому моменту все накопленные чанки показывали ОДИН И ТОТ ЖЕ
         (самый свежий) кусок: собеседник слышал 128 сэмплов, повторённые
         8 раз — жужжащий «бурундук» вместо голоса. Проверено в Chromium:
         dup_ratio 0.875 (7 из 8 чанков идентичны) -> 0 после фикса. */
      this.b.push(new Float32Array(ch)); this.n += ch.length;
      if (this.n >= 960){
        const o = new Float32Array(this.n);
        let k = 0;
        for (const c of this.b){ o.set(c, k); k += c.length; }
        this.b = []; this.n = 0;
        this.port.postMessage(o, [o.buffer]);
      }
    }
    return true;
  }
}

class VcPlay extends AudioWorkletProcessor {
  constructor(){
    super();
    this.q = []; this.tot = 0; this.pos = 0;
    this.started = false; this.needIn = false;
    this.targetS = Math.round(80 * sampleRate / 1000);
    this.port.onmessage = (e) => {
      if (e.data && e.data.targetMs !== undefined){
        this.targetS = Math.max(160, Math.round(e.data.targetMs * sampleRate / 1000));
        return;
      }
      const f = new Float32Array(e.data);
      this.q.push(f); this.tot += f.length;
      const cap = Math.round(600 * sampleRate / 1000);
      while (this.tot > cap && this.q.length > 1){
        this.tot -= this.q[0].length; this.q.shift(); this.pos = 0;
      }
    };
  }
  process(inputs, outputs){
    const out = outputs[0][0];
    const R = Math.max(16, Math.round(2 * sampleRate / 1000)); /* ~2мс */
    let i = 0;
    if (!this.started && this.tot >= this.targetS){
      this.started = true; this.needIn = true;
    }
    if (this.started){
      while (i < out.length && this.q.length){
        const fr = this.q[0];
        const need = Math.min(out.length - i, fr.length - this.pos);
        for (let k = 0; k < need; k++) out[i + k] = fr[this.pos + k];
        this.pos += need; i += need;
        if (this.pos >= fr.length){ this.tot -= fr.length; this.q.shift(); this.pos = 0; }
      }
      if (this.needIn && i > 0){       /* фейд-ин после паузы */
        const n = Math.min(R, i);
        for (let k = 0; k < n; k++) out[k] *= k / n;
        this.needIn = false;
      }
    }
    if (i < out.length){
      if (i > 0){                       /* фейд-аут хвоста — без щелчка */
        const n = Math.min(R, i);
        for (let k = 0; k < n; k++) out[i - 1 - k] *= k / n;
      }
      for (let k = i; k < out.length; k++) out[k] = 0;
      if (this.started){
        this.started = false;
        this.port.postMessage({under: true});
      }
    }
    return true;
  }
}

registerProcessor("vc-cap", VcCap);
registerProcessor("vc-play", VcPlay);
