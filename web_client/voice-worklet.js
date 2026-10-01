/* voice-worklet.js — AudioWorklet-процессоры голосового канала.
   Загружается через ctx.audioWorklet.addModule("/static/voice-worklet.js")
   из voice.js (раньше жил строкой-шаблоном в index.html и собирался в
   Blob — при разбиении клиента на модули переехал в настоящий файл).

   VcCap — захват: копит сэмплы и шлёт батчами по 20мс (960 сэмплов
   при 48кГц, v2.0).
   VcPlay — плейаут: джиттер-буфер с пре-буфером, PLC-достройка
   микрозазоров (v3.7.0), фейды ~2мс на стыках звук↔тишина (без
   щелчков «ТЦЦЦ»), дренирование задержки на тишине. */

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
    this.sil = [];          /* v3.7.0: «кадр — строгая тишина» (для дрена) */
    this.started = false; this.needIn = false;
    this.targetS = Math.round(80 * sampleRate / 1000);
    /* v3.7.0 PLC (packet loss concealment): пока кадр опаздывает, звук
       ДОСТРАИВАЕТСЯ затухающим повтором хвоста — микрозазоры 20-60мс
       перестают быть слышны («прерывистый звук, как старый скайп»).
       tail — кольцо последних ~6мс реального звука; plcRun — сколько
       сэмплов уже достроено в текущем зазоре; lastReal — последний
       реальный сэмпл (для стыка без щелчка). */
    this.TL = Math.max(128, Math.round(6 * sampleRate / 1000));
    this.PLC_MAX = Math.round(60 * sampleRate / 1000);
    this.tail = new Float32Array(this.TL);
    this.tailW = 0; this.plcRun = 0; this.lastReal = 0;
    this.unders = 0; this.procCount = 0;
    this.port.onmessage = (e) => {
      if (e.data && e.data.targetMs !== undefined){
        this.targetS = Math.max(160, Math.round(e.data.targetMs * sampleRate / 1000));
        return;
      }
      const f = new Float32Array(e.data);
      /* Строгая тишина? (микшер шлёт настоящие нули в паузах). Редкая
         выборка — точности достаточно, «тихая речь» нулем не будет. */
      let en = 0;
      for (let i = 0; i < f.length; i += 16){ const v = f[i]; en += v * v; }
      this.q.push(f); this.tot += f.length; this.sil.push(en === 0);
      /* v3.7.0 ДРЕН ЗАДЕРЖКИ: если буфер распух (>200мс — всплеск после
         затыка) и впереди тишина — молча пропускаем её: латентность
         гасится В ПАУЗАХ разговора, а не скачком посреди речи. */
      const highWater = Math.round(200 * sampleRate / 1000);
      while (this.q.length > 1 && this.tot > highWater && this.pos === 0
             && this.sil[0]){
        this.tot -= this.q[0].length; this.q.shift(); this.sil.shift();
      }
      /* Переполнение (дошло до 600мс) — режем ОДНИМ куском до 400мс:
         реже, чем по кадру, и каждый скачок дальше от начала речи. */
      const cap = Math.round(600 * sampleRate / 1000);
      if (this.tot > cap){
        const cut = this.tot - Math.round(400 * sampleRate / 1000);
        let acc = 0;
        while (this.q.length && acc < cut){
          acc += this.q[0].length; this.tot -= this.q[0].length;
          this.q.shift(); this.sil.shift();
        }
        this.pos = 0;
      }
    };
  }
  process(inputs, outputs){
    const out = outputs[0][0];
    const R = Math.max(16, Math.round(2 * sampleRate / 1000)); /* ~2мс */
    /* v3.7.0 ПОРОГ ПЕРЕЗАПУСКА = ПОЛОВИНА ЦЕЛИ. Раньше после каждого
       недобора плейаут ждал ПОЛНОЙ цели (80-360мс ТИШИНЫ): каждый затык
       сети звучал паузой в полбуфера — «лагает, как старый скайп».
       Теперь рестарт на targetS/2: затык до ~140мс не слышен ВООБЩЕ
       (половина буфера + 60мс PLC), а при больших — пауза вдвое короче.
       Подушка до цели добирается сама всплесками TCP. */
    const restartS = Math.max(160, this.targetS >> 1);
    let i = 0;
    if (!this.started && this.tot >= restartS){
      this.started = true; this.needIn = true;
    }
    if (this.started){
      while (i < out.length && this.q.length){
        const fr = this.q[0];
        const need = Math.min(out.length - i, fr.length - this.pos);
        for (let k = 0; k < need; k++){
          const v = fr[this.pos + k];
          out[i + k] = v;
          this.tail[this.tailW] = v;              /* хвост для PLC */
          this.tailW = (this.tailW + 1) % this.TL;
        }
        this.pos += need; i += need;
        if (this.pos >= fr.length){
          this.tot -= fr.length; this.q.shift(); this.sil.shift(); this.pos = 0;
        }
      }
      if (i > 0){ this.plcRun = 0; this.lastReal = out[i - 1]; }
      if (this.needIn && i > 0){       /* фейд-ин после паузы */
        const n = Math.min(R, i);
        for (let k = 0; k < n; k++) out[k] *= k / n;
        this.needIn = false;
      }
    }
    if (i < out.length){
      const dry = out.length - i;
      const budgetLeft = Math.max(0, this.PLC_MAX - this.plcRun);
      const fill = (this.started || this.plcRun > 0)
        ? Math.min(dry, budgetLeft) : 0;
      if (i > 0 && fill === 0){
        /* PLC не работает (исчерпан/не начат) — обычный фейд-аут хвоста */
        const n = Math.min(R, i);
        for (let k = 0; k < n; k++) out[i - 1 - k] *= k / n;
      }
      if (fill > 0){
        /* v3.7.0 PLC: достраиваем зазор повтором хвоста. Стык без щелчка:
           t < R — перекрёстный сплав «последний сэмпл → хвост», огибающая
           линейно до нуля к концу бюджета (в тишину — плавно). */
        for (let k = 0; k < fill; k++){
          const t = this.plcRun + k;
          const env = 1 - t / this.PLC_MAX;
          const tv = this.tail[(this.tailW + t) % this.TL];
          out[i + k] = t < R
            ? (this.lastReal * (1 - t / R) + tv * (t / R)) * env
            : tv * env;
        }
        this.plcRun += fill; i += fill;
      }
      for (let k = i; k < out.length; k++) out[k] = 0;
      if (this.started){
        this.started = false;
        this.unders++;
        this.port.postMessage({under: true});
      }
    }
    /* v3.7.0 телеметрия ~раз в секунду: уровень буфера + недоборы */
    if (++this.procCount >= Math.max(1, Math.round(sampleRate / 128))){
      this.procCount = 0;
      this.port.postMessage({stat: {lvl: this.tot, und: this.unders}});
    }
    return true;
  }
}

registerProcessor("vc-cap", VcCap);
registerProcessor("vc-play", VcPlay);
