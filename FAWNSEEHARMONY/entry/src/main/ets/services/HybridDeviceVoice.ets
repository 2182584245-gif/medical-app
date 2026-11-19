import { audio } from '@kit.AudioKit'
import { util } from '@kit.ArkTS'
import { actorId, cloud, guestVoice } from './Cloud'
import { requirePrivacy } from './PrivacyChoices'

type Message = Record<string, Object>
export type DeviceEmit = (event: string, value: Message) => void

let capture: audio.AudioCapturer | undefined
let generation: number = 0
let owner: number = -1
let parts: ArrayBuffer[] = []
let bytes: number = 0
let timeout: number = -1
let capturing: boolean = false
let starting: boolean = false
let finishing: Promise<Message> | undefined
let completed: Message | undefined
const limit = 16000 * 2 * 60

export function hybridVoiceStamp():number{return generation}

async function release(): Promise<void> {
  clearTimeout(timeout)
  const active = capture
  capture = undefined
  if (!active) return
  active.off('readData')
  try { if (active.state === audio.AudioState.STATE_RUNNING) await active.stop() }
  finally { await active.release() }
}

export async function abortHybridVoice(): Promise<void> {
  generation++
  parts = []
  bytes = 0
  starting = false
  capturing = false
  finishing = undefined
  completed = undefined
  await release()
}

function encodeWave(chunks: ArrayBuffer[], size: number): string {
  const data = new Uint8Array(44 + size)
  const view = new DataView(data.buffer)
  data.set([82,73,70,70],0); data.set([87,65,86,69,102,109,116,32],8); data.set([100,97,116,97],36)
  view.setUint32(4,36+size,true); view.setUint32(16,16,true); view.setUint16(20,1,true); view.setUint16(22,1,true)
  view.setUint32(24,16000,true); view.setUint32(28,32000,true); view.setUint16(32,2,true); view.setUint16(34,16,true); view.setUint32(40,size,true)
  let offset = 44
  for (const chunk of chunks) { data.set(new Uint8Array(chunk),offset); offset += chunk.byteLength }
  return new util.Base64Helper().encodeToStringSync(data)
}

export async function beginHybridVoice(seconds: number, emit: DeviceEmit): Promise<Message> {
  requirePrivacy()
  if (starting || capturing || finishing) throw new Error('语音正忙，请稍候。')
  starting = true
  completed = undefined
  const stamp = ++generation
  owner = actorId()
  try {
    const mic = await audio.createAudioCapturer({
      streamInfo: { samplingRate: audio.AudioSamplingRate.SAMPLE_RATE_16000, channels: audio.AudioChannel.CHANNEL_1,
        sampleFormat: audio.AudioSampleFormat.SAMPLE_FORMAT_S16LE, encodingType: audio.AudioEncodingType.ENCODING_TYPE_RAW },
      capturerInfo: { source: audio.SourceType.SOURCE_TYPE_MIC, capturerFlags: 0 }
    })
    if (stamp !== generation) { await mic.release(); throw new Error('录音已取消。') }
    capture = mic
    let lastLevel = 0
    mic.on('readData', (pcm: ArrayBuffer) => {
      if (stamp !== generation || bytes >= limit) return
      const chunk = pcm.slice(0,Math.min(pcm.byteLength,limit-bytes))
      parts.push(chunk); bytes += chunk.byteLength
      if (Date.now()-lastLevel < 80) return
      lastLevel = Date.now()
      const samples = new Int16Array(chunk,0,Math.floor(chunk.byteLength/2))
      let energy = 0
      for (let i=0;i<samples.length;i++) energy += samples[i]*samples[i]
      const level = Math.min(1,Math.sqrt(energy/Math.max(1,samples.length))/6000)
      emit('voiceLevel',{ 'session_id':stamp, 'level':level })
    })
    await mic.start()
    if (stamp !== generation) throw new Error('录音已取消。')
    capturing = true
    emit('voiceStarted',{'text':'','session_id':stamp})
    timeout = setTimeout(() => {
      if (capturing && stamp === generation) endHybridVoice(emit).catch(() => {})
    }, seconds*1000)
    return { 'recording':true, 'max_seconds':seconds, 'session_id':stamp }
  } catch (error) {
    if (stamp === generation) await abortHybridVoice()
    throw error as Error
  } finally { if (stamp === generation) starting = false }
}

async function transcribe(stamp:number, emit:DeviceEmit):Promise<Message> {
  await release()
  if (stamp!==generation || owner!==actorId()) throw new Error('录音会话已切换。')
  const length=bytes
  const data=parts
  parts=[]; bytes=0
  if (length<3200) throw new Error('录音时间太短，请再说一次。')
  emit('voiceProcessing',{'processing':true,'text':'','session_id':stamp,'duration_ms':Math.round(length/32)})
  const body:Message={'wav_base64':encodeWave(data,length)}
  const result:Message=owner>0 ? await cloud<Message>('/v1/member/voice/transcribe','POST',body) : await guestVoice(body)
  if (stamp!==generation || owner!==actorId()) throw new Error('录音会话已切换。')
  if (typeof result.text!=='string'||!(result.text as string).trim()) throw new Error('没有听清，请再说一次。')
  result['session_id']=stamp
  completed=result
  emit('voiceStopped',result)
  return result
}

export async function endHybridVoice(emit:DeviceEmit):Promise<Message> {
  if (finishing) return finishing
  if (completed) return completed
  if (!capturing) { await abortHybridVoice(); throw new Error('录音尚未开始，请重新按住说话。') }
  capturing=false
  const stamp=generation
  finishing=transcribe(stamp,emit).catch((error:Error) => {
    if (stamp===generation) emit('voiceError',{'message':error.message,'text':error.message,'code':'voice_failed','session_id':stamp})
    throw error
  })
  try {return await finishing} finally {if (stamp===generation) finishing=undefined}
}
