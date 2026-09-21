// Account-scoped receipts: no File/Blob data, signed URLs, or credentials.
const DB_NAME='tingji-upload-receipts',STORE='receipts',TIMEOUT_MS=2500;
let queue=Promise.resolve();
export function fileMetadata(files){return Array.from(files,file=>({name:file.name,size:file.size,lastModified:file.lastModified}));}
export function matchesOriginalFiles(files,expected){return files.length===expected.length&&files.every((file,i)=>file.name===expected[i].name&&file.size===expected[i].size&&file.lastModified===expected[i].lastModified);}
function receipt(value){
  if(!value||typeof value!=='object'||typeof value.userId!=='string'||!value.userId||value.userId.length>100)return null;
  const body=value.requestBody,files=value.files;
  if(!body||typeof body.requestId!=='string'||!body.requestId||body.requestId.length>80||!Array.isArray(files)||files.length<1||files.length>24||!Array.isArray(body.files)||body.files.length!==files.length)return null;
  if(!['combine','separate'].includes(body.mode)||!/^\d{4}-\d{2}-\d{2}$/.test(body.recordingDate)||typeof body.title!=='string'||body.title.length>160||!(body.courseId===null||typeof body.courseId==='string')||!(body.prompt===undefined||typeof body.prompt==='string'&&body.prompt.length<=30000))return null;
  if(!files.every((file,i)=>file&&typeof file.name==='string'&&file.name.length>0&&file.name.length<=255&&Number.isSafeInteger(file.size)&&file.size>0&&Number.isSafeInteger(file.lastModified)&&file.lastModified>=0&&body.files[i]?.filename===file.name&&body.files[i]?.size===file.size))return null;
  const requestBody={requestId:body.requestId,files:files.map(file=>({filename:file.name,size:file.size})),courseId:body.courseId,recordingDate:body.recordingDate,mode:body.mode,title:body.title};
  if(body.prompt!==undefined)requestBody.prompt=body.prompt;
  return {userId:value.userId,batchId:typeof value.batchId==='string'?value.batchId:'',requestBody,files:fileMetadata(files),done:[...new Set((Array.isArray(value.done)?value.done:[]).filter(i=>Number.isInteger(i)&&i>=0&&i<files.length))]};
}
function operation(mode,action){
  const run=()=>new Promise((resolve,reject)=>{
    let db,transaction,settled=false;
    const finish=(error,result)=>{if(settled)return;settled=true;clearTimeout(timer);db?.close();error?reject(error):resolve(result);};
    const timer=setTimeout(()=>{try{transaction?.abort();}catch{}finish(Error('浏览器保存上传进度超时。'));},TIMEOUT_MS);
    try{
      const open=indexedDB.open(DB_NAME,1);
      open.onupgradeneeded=()=>{if(!open.result.objectStoreNames.contains(STORE))open.result.createObjectStore(STORE,{keyPath:'userId'});};
      open.onerror=()=>finish(Error('浏览器无法保存上传进度。'));open.onblocked=()=>finish(Error('浏览器的上传进度存储暂不可用。'));
      open.onsuccess=()=>{db=open.result;if(settled){db.close();return;}try{transaction=db.transaction(STORE,mode);const request=action(transaction.objectStore(STORE));transaction.oncomplete=()=>finish(null,request?.result);transaction.onerror=transaction.onabort=()=>finish(Error('浏览器无法保存上传进度。'));}catch{finish(Error('浏览器无法保存上传进度。'));}};
    }catch{finish(Error('浏览器无法保存上传进度。'));}
  });
  const result=queue.then(run,run);queue=result.catch(()=>{});return result;
}
export const uploadDrafts={
  async load(userId){const value=receipt(await operation('readonly',store=>store.get(userId)));return value?.userId===userId?value:null;},
  async save(value){const safe=receipt(value);if(!safe)throw Error('上传进度信息不完整。');await operation('readwrite',store=>store.put(safe));},
  async clear(userId){if(userId)await operation('readwrite',store=>store.delete(userId));},
};
