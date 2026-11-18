import { audio } from '@kit.AudioKit'
import { abilityAccessCtrl, common } from '@kit.AbilityKit'
import { util } from '@kit.ArkTS'
import { actorId, cloud, guestVoice } from './Cloud'

let capture: audio.AudioCapturer|undefined
let serial = 0
let chunks: ArrayBuffer[] = []
let length = 0
let owner = 0
let timer: number = -1
const LIMIT = 16000 * 2 * 60
export async function cancelVoice(): Promise<void> {
  ++serial
  clearTimeout(timer)
  const active = capture
  capture = undefined
  chunks = []; length = 0
  if (active) {
    try { if (active.state === audio.AudioState.STATE_RUNNING) await active.stop() }
    finally { active.off('readData'); await active.release() }
  }
}
export async function startVoice(context: common.UIAbilityContext): Promise<void> {
  await cancelVoice()
  const key = serial
  owner = actorId()
  const permission = await abilityAccessCtrl.createAtManager().requestPermissionsFromUser(context,['ohos.permission.MICROPHONE'])
  if (permission.authResults[0] !== 0) throw new Error('请允许使用麦克风后，再按住说话。')
  if (key !== serial) return
  const mic = await audio.createAudioCapturer({
    streamInfo: { samplingRate:audio.AudioSamplingRate.SAMPLE_RATE_16000, channels:audio.AudioChannel.CHANNEL_1,
      sampleFormat:audio.AudioSampleFormat.SAMPLE_FORMAT_S16LE, encodingType:audio.AudioEncodingType.ENCODING_TYPE_RAW },
    capturerInfo:{ source:audio.SourceType.SOURCE_TYPE_MIC,capturerFlags:0 }
  })
  if(key !== serial) { await mic.release(); return }
  capture = mic
  mic.on('readData',(data:ArrayBuffer) => {
    if(key !== serial || length >= LIMIT) return
    const next = data.slice(0,Math.min(data.byteLength,LIMIT-length))
    chunks.push(next); length += next.byteLength
  })
  try {
    await mic.start()
    if (key === serial) timer = setTimeout(() => { if (key === serial && mic.state === audio.AudioState.STATE_RUNNING) mic.stop().catch(() => {}) },60000)
  } catch(e) { if (key === serial) await cancelVoice(); throw e as Error }
}
function wave(parts:ArrayBuffer[],total:number): Uint8Array {
  const buffer = new ArrayBuffer(44+total)
  const bytes = new Uint8Array(buffer)
  bytes.set([82,73,70,70],0);bytes.set([87,65,86,69,102,109,116,32],8);bytes.set([100,97,116,97],36)
  const view=new DataView(buffer)
  view.setUint32(4,36+total,true);view.setUint32(16,16,true);view.setUint16(20,1,true);view.setUint16(22,1,true)
  view.setUint32(24,16000,true);view.setUint32(28,32000,true);view.setUint16(32,2,true);view.setUint16(34,16,true);view.setUint32(40,total,true)
  let at=44
  for(const part of parts){bytes.set(new Uint8Array(part),at);at+=part.byteLength}
  return bytes
}
export async function finishVoice(): Promise<string> {
  if(!capture){await cancelVoice();throw new Error('录音还未开始，请按住后说话。')}
  const data=chunks;const size=length;const user=owner
  await cancelVoice()
  if(size<3200) throw new Error('录音时间太短，请再说一次。')
  if (actorId() !== user) throw new Error('账号已切换。')
  const encoded=new util.Base64Helper().encodeToStringSync(wave(data,size))
  const reply=user ? await cloud<Record<string,string>>('/v1/member/voice/transcribe','POST',{'wav_base64':encoded} as Record<string,string>) : await guestVoice({'wav_base64':encoded} as Record<string,string>)
  if(!reply.text?.trim()) throw new Error('没有听清，请再说一次。')
  return reply.text.trim()
}
