from fastapi import APIRouter, Request, Body
from app.config import MODULES
from app.engine import evaluate
from app.web import render
router=APIRouter()
@router.get('/allocation')
def page(request:Request):
    return render(request,'allocation')
@router.post('/api/allocation')
def calculate(payload:dict=Body(default={})):
    return evaluate(payload,until=MODULES.index('allocation')+1)
