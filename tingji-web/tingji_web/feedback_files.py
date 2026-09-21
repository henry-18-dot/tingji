"""Small authenticated feedback attachments; never execute or extract uploads."""
import base64
import binascii
import io
from pathlib import PurePath
from urllib.parse import quote
import zipfile
from fastapi import HTTPException
from fastapi.responses import Response
from PIL import Image
from sqlalchemy import select
from .preferences_models import FeedbackAttachment

MAX_TOTAL = 3 * 1024 * 1024
TYPES = {'.pdf':'application/pdf','.txt':'text/plain','.md':'text/plain','.csv':'text/plain',
         '.doc':'application/msword','.xls':'application/vnd.ms-excel','.ppt':'application/vnd.ms-powerpoint',
         '.docx':'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
         '.xlsx':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
         '.pptx':'application/vnd.openxmlformats-officedocument.presentationml.presentation',
         '.png':'image/png','.jpg':'image/jpeg','.jpeg':'image/jpeg','.webp':'image/webp','.gif':'image/gif'}

def validate_files(items):
    if len(items)>4: raise HTTPException(422,'最多附 4 个文件。')
    result=[]; total=0
    for item in items:
        name=PurePath(item.filename.replace('\\','/')).name
        name=''.join(c for c in name if ord(c)>=32)[:180]
        suffix=PurePath(name).suffix.lower()
        if suffix not in TYPES: raise HTTPException(422,'支持图片、PDF、Word、PPT、Excel 和文本文件。')
        try: data=base64.b64decode(item.contentBase64,validate=True)
        except (ValueError,binascii.Error): raise HTTPException(422,'附件内容无效，请重新选择。')
        total+=len(data)
        if not data or total>MAX_TOTAL: raise HTTPException(413,'附件合计最多 3 MB。')
        media=TYPES[suffix]
        try:
            if media.startswith('image/'):
                image=Image.open(io.BytesIO(data)); actual=image.format; image.verify()
                if actual not in {'PNG','JPEG','WEBP','GIF'} or media!=Image.MIME.get(actual): raise ValueError()
            elif suffix=='.pdf':
                if not data.startswith(b'%PDF-'): raise ValueError()
            elif suffix in {'.doc','.xls','.ppt'}:
                if not data.startswith(bytes.fromhex('D0CF11E0A1B11AE1')): raise ValueError()
            elif suffix in {'.docx','.xlsx','.pptx'}:
                with zipfile.ZipFile(io.BytesIO(data)) as z:
                    if '[Content_Types].xml' not in z.namelist() or sum(i.file_size for i in z.infolist())>50*1024*1024: raise ValueError()
            else: data.decode('utf-8-sig')
        except Exception: raise HTTPException(422,'附件格式与文件名不符，或文件已损坏。')
        result.append((name,media,data))
    return result

def attachment_json(row):
    return {'id':row.id,'filename':row.filename,'size':row.size,'isImage':row.media_type.startswith('image/'),'url':f'/api/feedback/files/{row.id}'}

def attachments_for(db,feedback_id):
    return [attachment_json(a) for a in db.scalars(select(FeedbackAttachment).where(FeedbackAttachment.feedback_id==feedback_id))]

def attachment_response(db,user,file_id):
    row=db.get(FeedbackAttachment,file_id)
    if not row or (row.user_id!=user.id and not user.is_admin): raise HTTPException(404,'找不到附件。')
    mode='inline' if row.media_type.startswith('image/') else 'attachment'
    return Response(row.data,media_type=row.media_type,headers={'Content-Disposition':f"{mode}; filename*=UTF-8\'\'{quote(row.filename)}",'X-Content-Type-Options':'nosniff','Cache-Control':'private, no-store','Content-Security-Policy':"default-src 'none'; sandbox"})
