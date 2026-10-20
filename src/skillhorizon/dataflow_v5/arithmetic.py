"""Fixed arithmetic only. Input selection stays with the caller."""
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP

def normalize(source, contract, mapping, policy):
 cols=contract['columns']; records={}
 for raw in source['rows']:
  record={key:raw[col] for key,col in cols.items()}
  key=record['order']; version=int(record['version'])
  if key in records:
   if policy=='first':continue
   if policy=='latest_revision' and int(records[key]['version'])>=version:continue
   if record['kind']=='metadata':continue
  records[key]=record
 result=[]
 for key,row in sorted(records.items()):
  if row['kind']=='withdrawal':continue
  amount=Decimal(str(row['amount']))*(100 if contract['unit']=='major' else 1)
  cents=int((amount*Decimal(contract['rate_numerator'])/Decimal(contract['rate_denominator'])).quantize(Decimal('1'),rounding=ROUND_HALF_UP))
  result.append(dict(order=key,account=row['account'],entity=mapping['entities'][row['account']],cents=cents,version=int(row['version'])))
 return result

def aggregate(rows):
 out=defaultdict(int)
 for r in rows:out[r['entity']]+=r['cents']
 return [dict(entity=e,cents=c) for e,c in sorted(out.items())]
