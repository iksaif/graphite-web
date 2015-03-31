import socket
import time
import httplib
from urllib import urlencode
from django.core.cache import cache
from django.conf import settings
from graphite.render.hashing import compactHash
from graphite.util import unpickle
from graphite.logger import log
try:
  import simplejson as json
except ImportError:
  import json

import itertools
import re
from datetime import datetime



class RemoteStore(object):
  lastFailure = 0.0
  retryDelay = settings.REMOTE_STORE_RETRY_DELAY
  available = property(lambda self: time.time() - self.lastFailure > self.retryDelay)

  def __init__(self, host):
    self.host = host


  def find(self, query):
    request = FindRequest(self, query)
    request.send()
    return request


  def fail(self):
    self.lastFailure = time.time()



class FindRequest:
  suppressErrors = True

  def __init__(self, store, query):
    self.store = store
    self.query = query
    self.connection = None
    self.cacheKey = compactHash('find:%s:%s' % (self.store.host, query))
    self.cachedResults = None
    self.agglo = re.compile("^criteo.agglo.(.*).all$")
    self.is_agglo = False

  @classmethod
  def glob(fr, idx, val):
    parts = [ el.split(',') for el in re.compile('{([^}]+)}').split(val)]
    globbed =  [''.join(l) for l in itertools.product(*parts)]
    disj = [{'wildcard' : {'field%d' % idx : el }} for el in globbed]
    return { "bool" : { "should" : disj } }

  @classmethod
  def create_filters(fr, idx, val):
    queries = []
    filters = []
    if ('{' in val and '}' in val):
      qs = FindRequest.glob(idx, val)
      return ([qs], [])
    if '*' == val:
      return ([], [])
    if '*' in val:
      queries.append({'wildcard' : {'field%d' % idx : val }})
    else:
      filters.append({'term' : {'field%d' % idx : val }})
    return (queries, filters)



  def send(self):
    self.cachedResults = cache.get(self.cacheKey)

    if self.cachedResults:
      return

    self.connection = HTTPConnectionWithTimeout(self.store.host, 9200)
    self.connection.timeout = settings.REMOTE_STORE_FIND_TIMEOUT

    self.query.count('.')

    try:
      url =  'sagitarius-metrics/metadata/_search?'

      queries = []
      filters = []
      nw_query = self.agglo.sub(r'criteo.\1.*', self.query)
      self.is_agglo = nw_query != self.query
      self.query = nw_query
      subQueries = self.query.split('.')
      token_count = len(subQueries)
      filters.append({'range' : {'token_count' : { 'gte' : token_count } }})
      for idx, val in enumerate(subQueries):
        qs, fs = FindRequest.create_filters(idx, val)
        log.info(qs)
        queries.extend(qs)
        filters.extend(fs)

      post_body = {}
      if len(filters) > 0:
        post_body['filter'] = { 'and' : filters }
      if len(queries) > 0:
        post_body['query'] = { 'bool' : {'must' : queries}}
      post_body['size'] = 100

      post_body['sort'] = [ { 'token_count': 'asc' }]

      body = json.dumps(post_body)
      headers = {
          'Content-Type': 'application/x-www-form-urlencoded',
          'Accept' : '*/*'
          }
      #self.connection.set_debuglevel(1)
      log.info("curl -XPOST '%s:%d/%s&pretty' -d '%s'" % (self.store.host, 9200, url, body))
      self.connection.request('POST', url, body, headers)
    except Exception as e :
      log.info("failed")
      log.info(str(e))
      self.store.fail()
      if not self.suppressErrors:
        raise


  def get_results(self):
    if self.cachedResults:
      return self.cachedResults

    if not self.connection:
      self.send()

    try:
      response = self.connection.getresponse()
      assert response.status == 200, "received error response %s - %s" % (response.status, response.reason)
      result_data = json.loads(response.read())

      counts = [ r['_source']['token_count'] for r in result_data['hits']['hits'] ]
      min_count = min(counts) if len(counts) > 0 else 0

      hits = [ r for r in result_data['hits']['hits'] if r['_source']['token_count'] == min_count ]

      if self.is_agglo:
        log.info("agglo mode")
        # projection on n-1 hyperplan # dedicace v.jacques
        to_key = lambda obj: [obj['_source']['field%d' % j] for j in range(min_count-1)]

        results = []
        for (group, vs) in itertools.groupby(hits, to_key):
          values = list(vs)
          path = group
          path.insert(1, 'agglo')
          path.append('all')
          results.append({ 'isLeaf' : values[0]['_source']['leaf'], 'metric_path' : '.'.join(path), '_ids' : [int(r['_id']) for r in values] })
      else:
        results = [ {'isLeaf': r['_source']['leaf'], 'metric_path' : r['_source']['path'], '_ids': [int(r['_id'])]} for r in hits ]

    except:
      self.store.fail()
      if not self.suppressErrors:
        raise
      else:
        results = []

    resultNodes = [ RemoteNode(self.store, node['metric_path'], node['isLeaf'], node['_ids']) for node in results ]
    cache.set(self.cacheKey, resultNodes, settings.REMOTE_FIND_CACHE_DURATION)
    self.cachedResults = resultNodes
    return resultNodes



class RemoteNode:
  context = {}

  def __init__(self, store, metric_path, isLeaf, ids):
    self.ids = ids
    self.store = store
    self.fs_path = None
    self.metric_path = metric_path
    self.real_metric = metric_path
    self.name = metric_path.split('.')[-1]
    self.__isLeaf = isLeaf
    self.step = 60000

  def logcheck(self, start, point, desc=""):
    delta = datetime.now() - start
    duration = delta.microseconds + delta.seconds * 1000000 # ignoring delta.days
    RemoteNode.time[point] += duration
    until_now  = RemoteNode.time[point] - RemoteNode.time[point -1 ] if point > 0 else RemoteNode.time[point]
    log.info('%d (%s) took until now : %d micros' % (point, desc, until_now))
    if len(self.ids) == 1:
      id = self.ids[0]
    else:
      id = self.metric_path
    log.info('parent(s): %s, time: %f, checkpoint %d' % (id, duration / 1000, point))

  time = [0 for i in range(100)]

  def create_body(self, startTime, endTime):
    if len(self.ids) == 1:
      body = {'query': {'term': {'parent': self.ids[0]}}}
    else:
      body = {'query': {'terms': {'parent': self.ids, 'minimum_should_match': 1}}}

    aggs = {"by_time": {
      "histogram": {"field": "ts", "interval": self.step, "min_doc_count": 0, "extended_bounds" : {"min": startTime *1000, "max": endTime * 1000}},
      "aggs": {"sum": {"sum": {"field": "value"}}}
      }}
    aggs = { 'timefilter' : {
      'filter' : { 'range' : { 'ts' : { 'from' :  startTime *1000, 'to': endTime * 1000 }}},
      'aggs' : aggs
      }}
    body['aggs'] = aggs
    return body


  def fetch(self, startTime, endTime, requestContext):
    if not self.__isLeaf:
      return []
    start = datetime.now()

    self.logcheck(start, 0)

    connection = HTTPConnectionWithTimeout(self.store.host, 9200)
    connection.timeout = settings.REMOTE_STORE_FETCH_TIMEOUT
    url = 'sagitarius/metric/_search?search_type=count'
    self.logcheck(start, 1, "json creation")
    body = self.create_body(startTime, endTime)
    post_body = json.dumps(body)

    log.info("curl -XPOST '%s:%d/%s&pretty' -d '%s'" % (self.store.host, 9200, url, post_body))

    self.logcheck(start, 2, "json dump")
    connection.request('POST', url, post_body)
    self.logcheck(start, 3, "post request")
    response = connection.getresponse()
    self.logcheck(start, 4, "get response")
    assert response.status == 200, "Failed to retrieve remote data: %d %s" % (response.status, response.reason)
    self.logcheck(start, 5, "check status")
    rawData = response.read()
    self.logcheck(start, 6, "read response")
    seriesList = json.loads(rawData)
    self.logcheck(start, 7, "parse json")

    timebuckets = seriesList['aggregations']['timefilter']['by_time']['buckets']
    expected_points = (int(endTime) - int(startTime)) *1000 / self.step
    log.info("took: %d, hits: %d, expected_points: %d, results: %d" % (seriesList['took'], seriesList['hits']['total'], expected_points,len(timebuckets)))
    timestamps = [ d['key'] / 1000 for d in timebuckets ]
    values = dict((d['key'] / 1000, d['sum']['value'] if d['doc_count'] > 0 else None)  for d in timebuckets)
    values = [ d['sum']['value'] if d['doc_count'] > 0 else None  for d in timebuckets]

    timeInfo = (int(startTime), int(endTime), self.step / 1000)
    self.logcheck(start, 8, "use results")

    return (timeInfo, values)

  def isLeaf(self):
    return self.__isLeaf

  def isLocal(self):
    return False



# This is a hack to put a timeout in the connect() of an HTTP request.
# Python 2.6 supports this already, but many Graphite installations
# are not on 2.6 yet.

class HTTPConnectionWithTimeout(httplib.HTTPConnection):
  timeout = 30

  def connect(self):
    msg = "getaddrinfo returns an empty list"
    for res in socket.getaddrinfo(self.host, self.port, 0, socket.SOCK_STREAM):
      af, socktype, proto, canonname, sa = res
      try:
        self.sock = socket.socket(af, socktype, proto)
        try:
          self.sock.settimeout( float(self.timeout) ) # default self.timeout is an object() in 2.6
        except:
          pass
        self.sock.connect(sa)
        self.sock.settimeout(None)
      except socket.error, msg:
        if self.sock:
          self.sock.close()
          self.sock = None
          continue
      break
    if not self.sock:
      raise socket.error, msg
