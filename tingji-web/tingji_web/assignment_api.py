"""Coursework listing, free rescan, and persistent manual corrections."""
from datetime import date
from typing import Literal
from fastapi import APIRouter,Depends,HTTPException
from pydantic import BaseModel,ConfigDict,Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from .auth import require_csrf,require_user
from .database import get_db
from .models import User
from .assignment_models import Assignment
from .assignments import assignments_json,assignment_json,scan_user_assignments

router=APIRouter(prefix='/api/assignments',tags=['assignments'])


class AssignmentPatch(BaseModel):
    model_config=ConfigDict(extra='forbid')
    title:str|None=Field(default=None,min_length=1,max_length=160)
    details:str|None=Field(default=None,max_length=5000)
    dueDate:str|None=None
    dueTime:str|None=Field(default=None,pattern=r'^(?:[01]\d|2[0-3]):[0-5]\d$|^24:00$')
    status:Literal['open','done']|None=None


@router.get('')
def list_assignments(user:User=Depends(require_user),db:Session=Depends(get_db)):
    return {'assignments':assignments_json(db,user.id)}


@router.post('/scan',dependencies=[Depends(require_csrf)])
def scan(user:User=Depends(require_user),db:Session=Depends(get_db)):
    count=scan_user_assignments(db,user.id);db.commit()
    return {'assignments':assignments_json(db,user.id),'recognized':count}


@router.patch('/{assignment_id}',dependencies=[Depends(require_csrf)])
def patch(assignment_id:str,body:AssignmentPatch,user:User=Depends(require_user),db:Session=Depends(get_db)):
    row=db.scalar(select(Assignment).where(Assignment.id==assignment_id,Assignment.user_id==user.id).with_for_update())
    if row is None:raise HTTPException(404,'找不到这项作业。')
    data=body.model_dump(exclude_unset=True)
    if data.get('dueDate'):
        try:data['dueDate']=date.fromisoformat(data['dueDate']).isoformat()
        except ValueError:raise HTTPException(400,'截止日期格式应为 YYYY-MM-DD。') from None
    for key,field in [('title','title'),('details','details'),('dueDate','due_date'),('dueTime','due_time'),('status','status')]:
        if key in data:
            if key in {'title','details','status'} and data[key] is None:raise HTTPException(400,'作业内容与状态不能为空。')
            setattr(row,field,data[key] or None if key in {'dueDate','dueTime'} else data[key])
    if set(data)-{'status'}:row.user_edited=True
    db.commit();return assignment_json(row)
