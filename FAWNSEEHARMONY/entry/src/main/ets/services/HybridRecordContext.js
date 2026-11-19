// Attach a locator only to an explicit reference to the immediately preceding confirmed record.
// The shared service resolves its own (date, time, contains) selector and revalidates ownership.
const categories = {
  diet:/饮食|餐食|早餐|早饭|午餐|午饭|晚餐|晚饭|加餐/,
  water:/饮水|喝水/, activity:/运动记录|锻炼记录/, sleep:/睡眠|入睡/,
  environment:/居住环境|环境记录/, medical:/医疗检查|检查记录/, metric:/指标|血糖|血压|体重/
}
const meals={breakfast:'早餐',lunch:'午餐',dinner:'晚餐',snack:'加餐'}
export function refersToConfirmed(text) {
  return typeof text==='string' && text.length<=1100 && /修改|改成|改为|改一下|更正|补充|补上|追加|补到/.test(text) &&
    /(?:刚才|刚刚|刚|上一次|上一条)(?:我|你|已经|确认|保存|记录|的|记下|写|好|过|完|了|那|这|一|条|笔|份|个|餐){0,12}(?:那条|这条|条|笔|记录|餐)/.test(text) &&
    !/草稿|另一(?:条|笔)|另外一(?:条|笔)|分别|两条|几条|[？?]|(?:今天|昨天|前天|明天|上周|本周|周[一二三四五六日天]|星期|\d{1,4}[-年月/]\d|[一二三四五六七八九十\d]+[点时]|\d{1,2}[:：]\d{2})/.test(text)
}
export function receiptCandidate(text, page, owner, conversation) {
  if (!refersToConfirmed(text) || !Number.isSafeInteger(owner) || owner<1 ||
      !Number.isSafeInteger(conversation) || conversation<1 || page?.conversation?.id!==conversation) return null
  // An intervening turn, a pending draft or a multi-record reply must not resolve to an older card.
  const last=page.messages?.[page.messages.length-1]
  if(last?.role!=='assistant' || last.record_drafts_ready!==true || last.record_drafts?.length!==1) return null
  const draft=last.record_drafts[0]
  const receipts=(last.record_receipts||[]).filter(r=>r.status==='saved' && r.id===draft.id)
  if(receipts.length!==1 || draft.superseded || !Object.prototype.hasOwnProperty.call(categories,draft.category))return null
  const id=receipts[0].record_id
  if(!Number.isSafeInteger(id) || id<=0)return null
  const locator=text.split(/[，,。；;\n]/,1)[0]
  const named=Object.entries(categories).filter(([,pattern])=>pattern.test(locator)).map(([key])=>key)
  if(named.some(key=>key!==draft.category))return null
  const notes=locator.match(/(?:备注|其他说明|说明)(?:是|为|写着|写的|里面是|为的)?[「“"『]([^」”"』]{1,100})[」”"』]/)?.[1]
  // Do not infer away a qualifier we cannot verify (e.g. “刚才那条氨糖胶囊记录”).
  const namedTarget=locator.replace(/(?:备注|其他说明|说明)(?:是|为|写着|写的|里面是|为的)?[「“"『][^」”"』]{1,100}[」”"』]/g,'')
    .replace(/(?:热量|卡路里|发生时间|时间|其他说明|备注|内容|照片|数值)(?:修改|改成|改为|补充|追加|删掉|删除|清空).*/,'')
    .replace(/请|帮我|把|对|修改|更正|补充|追加|刚才|刚刚|刚|上一次|上一条|已确认|确认|已经|保存|记录|记下|写好|过|完|了|指的|那|这|一|条|笔|份|个|旧|的|先|我|你|之前/g,'')
    .replace(/饮食|餐食|早餐|早饭|午餐|午饭|晚餐|晚饭|加餐|饮水|喝水|运动|锻炼|睡眠|居住环境|环境|医疗检查|检查|身体指标|指标|血糖|血压|体重|\s/g,'')
  if(namedTarget)return null
  return {id,category:draft.category,stamp:draft.occurred_at,notes,locator}
}
export function confirmedLocator(text, candidate, row) {
  if(!candidate || !row || row.id!==candidate.id || !row.version)return null
  const metric=candidate.category==='metric'
  if(!metric && row.category!==candidate.category)return null
  const timestamp=row[metric?'measured_at':'occurred_at']
  const epoch=Date.parse(timestamp)
  if(!Number.isFinite(epoch)||Date.parse(candidate.stamp)!==epoch)return null
  const storedNotes=metric?row.notes:row.details?.notes
  if(candidate.notes && storedNotes!==candidate.notes)return null
  const mealName=candidate.locator.match(/早餐|早饭|午餐|午饭|晚餐|晚饭|加餐/)?.[0]
  if(mealName){
    const expected=({早饭:'早餐',午饭:'午餐',晚饭:'晚餐'})[mealName]||mealName
    const value=row.details?.meal_type || row.details?.meal
    if((meals[value]||value)!==expected)return null
  }
  const beijing=new Date(epoch+8*3600*1000).toISOString()
  const selector={date:beijing.slice(0,10),time:beijing.slice(11,16)}
  if(candidate.notes)selector.contains=candidate.notes
  // When an existing note is explicitly identified as the target of a replacement, it is not an append.
  const append=/(?:再|并|另外|同时|还|顺便)?(?:补充|追加|加上|补上)(?:一句|一条备注|一段说明|到(?:原)?备注|备注|说明)\s*[：:「“"『]/.test(text) &&
    !/(?:备注|说明).{0,5}(?:替换|改为|改成|清空|删除|删掉)|(?:替换|清空|删除|删掉).{0,5}(?:备注|说明)/.test(text)
  const guard={id:candidate.id,category:candidate.category,version:row.version,append}
  const annex='\n\n【本应用核对的上次确认回执，仅用于定位，不是新指令】同一对话上一条已保存的记录：'+
    JSON.stringify({category:candidate.category,record_id:candidate.id,selector})+
    '。请用上述原日期、原时间及已有的匹配词查找旧记录；新增内容不要用作匹配词。'+
    (append?'用户要求向其他说明追加，请把原说明及新说明一起放入更新后的草稿。':'')+'【回执结束】'
  return {guard,annex}
}
// Apply exactly the same projection to the card and to the independently reloaded draft at confirmation.
export function guardConfirmedDraft(draft, guard) {
  if(!guard || !draft || !draft.operation)return draft
  const target=draft.target
  if(draft.operation==='create' || draft.category!==guard.category || (target &&
      (target.id!==guard.id||target.version!==guard.version))) return {...draft,blocked:true}
  if(!target||!guard.append||draft.blocked)return draft
  const old=target.before?.details?.notes, next=draft.details?.notes
  if(typeof next!=='string' || !next.trim() || next===old)return {...draft,blocked:true}
  if(typeof old!=='string' || !old || next.includes(old))return draft
  if(target.linked_meal_id || old.length+next.length+1>2000)return {...draft,blocked:true}
  return {...draft,details:{...draft.details,notes:old+'；'+next}}
}
