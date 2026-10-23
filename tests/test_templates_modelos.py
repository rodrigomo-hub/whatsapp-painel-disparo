import unittest
from fastapi import HTTPException
import templates_lote as tl

class ModelosTests(unittest.TestCase):
 def template(self):
  return {'name':'teste','language':'pt_BR','category':'UTILITY','status':'APPROVED','components':[{'type':'HEADER','format':'TEXT','text':'Atualização'},{'type':'BODY','text':'Olá {{1}}','example':{'body_text':[['Maria']]}},{'type':'BUTTONS','buttons':[{'type':'QUICK_REPLY','text':'Não quero'}]}]}
 def test_preserva_conteudo(self):
  p=tl.modelo_aprovado(self.template());v=tl.validar(dict(p,wabas=['123']));c=tl.componentes(v)
  self.assertEqual(c[0]['text'],'Atualização');self.assertEqual(c[1]['example']['body_text'],[['Maria']]);self.assertEqual(c[2]['buttons'][0]['text'],'Não quero')
 def test_imagem_obrigatoria(self):
  t=self.template();t['components'][0]={'type':'HEADER','format':'IMAGE','example':{'header_handle':['remote']}}
  p=tl.modelo_aprovado(t);self.assertTrue(p['image_required']);self.assertEqual(p['image'],'')
  with self.assertRaises(HTTPException):tl.validar(dict(p,wabas=['123']))
 def test_nao_descarta_formatos_nao_suportados(self):
  for component in [{'type':'CAROUSEL'},{'type':'HEADER','format':'VIDEO'},{'type':'BUTTONS','buttons':[{'type':'URL','text':'Abrir','url':'https://a.test/{{1}}'}]}]:
   t=self.template();t['components']=[t['components'][1],component]
   with self.assertRaises(HTTPException):tl.modelo_aprovado(t)
 def test_nao_aprovado(self):
  t=self.template();t['status']='PAUSED'
  with self.assertRaises(HTTPException):tl.modelo_aprovado(t)
