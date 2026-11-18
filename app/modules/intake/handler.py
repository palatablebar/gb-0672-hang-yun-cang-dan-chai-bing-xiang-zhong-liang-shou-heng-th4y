from fastapi import APIRouter, Request, Body
from app.config import MODULES
from app.engine import evaluate
from app.web import render
router=APIRouter()
@router.get('/intake')
def page(request:Request):
    return render(request,'intake')
@router.post('/api/intake')
def calculate(payload:dict=Body(default={})):
    return evaluate(payload,until=MODULES.index('intake')+1)
