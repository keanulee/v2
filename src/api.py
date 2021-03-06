from google.appengine.ext import ndb
from google.appengine.api import search

import json
import re
import webapp2

from datamodel import Library, Version, Content, Dependency
import versiontag


TIME_FORMAT = '%Y-%m-%dT%H:%M:%SZ'

def brief_metadata_from_searchdoc(document):
  result = {}
  for field in document.fields:
    if field.name == 'full_name':
      match = re.match(r'(.*)/(.*)', field.value)
      result['owner'] = match.group(1)
      result['repo'] = match.group(2)
    if field.name in ['owner', 'repo', 'repoparts']:
      continue
    if field.name == 'updated_at':
      result[field.name] = field.value.strftime(TIME_FORMAT)
    else:
      result[field.name] = field.value
  return result

# TODO(shans): This is expensive. We can
# a) eliminate it in the common case where the requested version is the most recent, as we can
#    directly extract the metadata from the index using briefMetaDataFromSearchDocument.
# b) amortize the rare case where the requested version is not the most recent, by indexing that
#    version once into a secondary index (which we don't search over), *then* using
#    briefMetaDataFromSearchDocument.
def brief_metadata_from_datastore(owner, repo, version):
  key = ndb.Key(Library, "%s/%s" % (owner.lower(), repo.lower()))
  library = key.get(read_policy=ndb.EVENTUAL_CONSISTENCY)
  metadata = json.loads(library.metadata)
  bower_key = ndb.Key(Library, "%s/%s" % (owner.lower(), repo.lower()), Version, version, Content, "bower.json")
  bower = bower_key.get(read_policy=ndb.EVENTUAL_CONSISTENCY)
  if not bower is None:
    bower = json.loads(bower.content)
  else:
    bower = {}
  description = bower.get('description', metadata.get('description', ''))
  return {
      'owner': owner,
      'repo': repo,
      'version': version,
      'description': description,
      'keywords': ' '.join(bower.get('keywords', [])),
      'stars': metadata.get('stargazers_count'),
      'subscribers': metadata.get('subscribers_count'),
      'forks': metadata.get('forks'),
      'contributors': library.contributor_count,
      'updated_at': metadata.get('updated_at')
  }

class SearchContents(webapp2.RequestHandler):
  def get(self, terms):
    index = search.Index('repo')
    limit = int(self.request.get('limit', 20))
    offset = int(self.request.get('offset', 0))
    search_results = index.search(
        search.Query(query_string=terms,
                     options=search.QueryOptions(limit=limit, offset=offset)))
    results = []
    for result in search_results.results:
      results.append(brief_metadata_from_searchdoc(result))
    self.response.headers['Access-Control-Allow-Origin'] = '*'
    self.response.write(json.dumps(results))

class GetDataMeta(webapp2.RequestHandler):
  def get(self, owner, repo, ver=None):
    owner = owner.lower()
    repo = repo.lower()
    library = Library.get_by_id('%s/%s' % (owner, repo), read_policy=ndb.EVENTUAL_CONSISTENCY)
    if library is None or library.error is not None:
      self.response.write(str(library))
      self.response.set_status(404)
      return
    versions = library.versions()
    if ver is None:
      ver = versions[-1]
    version = Version.get_by_id(ver, parent=library.key, read_policy=ndb.EVENTUAL_CONSISTENCY)
    if version is None or version.error is not None:
      self.response.write(str(version))
      self.response.set_status(404)
      return
    metadata = json.loads(library.metadata)
    dependencies = []
    bower = Content.get_by_id('bower', parent=version.key, read_policy=ndb.EVENTUAL_CONSISTENCY)
    if bower is not None:
      try:
        bower_json = json.loads(bower.content)
      # TODO: Which exception is this for?
      # pylint: disable=bare-except
      except:
        bower_json = {}
    readme = Content.get_by_id('readme.html', parent=version.key, read_policy=ndb.EVENTUAL_CONSISTENCY)
    full_name_match = re.match(r'(.*)/(.*)', metadata['full_name'])
    result = {
        'version': ver,
        'versions': versions,
        'readme': None if readme is None else readme.content,
        'subscribers': metadata['subscribers_count'],
        'stars': metadata['stargazers_count'],
        'forks': metadata['forks'],
        'contributors': library.contributor_count,
        'open_issues': metadata['open_issues'],
        'updated_at': metadata['updated_at'],
        'owner': full_name_match.groups()[0],
        'repo': full_name_match.groups()[1],
        'bower': None if bower is None else {
            'description': bower_json.get('description', ''),
            'license': bower_json.get('license', ''),
            'dependencies': bower_json.get('dependencies', []),
            'keywords': bower_json.get('keywords', []),
        },
        'collections': []
    }
    for collection in library.collections:
      if not versiontag.match(ver, collection.semver):
        continue
      collection_version = collection.version.id()
      collection_library = collection.version.parent().get()
      collection_metadata = json.loads(collection_library.metadata)
      collection_name_match = re.match(r'(.*)/(.*)', collection_metadata['full_name'])
      result['collections'].append({
          'owner': collection_name_match.groups()[0],
          'repo': collection_name_match.groups()[1],
          'version': collection_version
      })
    if library.kind == 'collection':
      dependencies = []
      version_futures = []
      for dep in version.dependencies:
        parsed_dep = Dependency.fromString(dep)
        dep_key = ndb.Key(Library, "%s/%s" % (parsed_dep.owner.lower(), parsed_dep.repo.lower()))
        version_futures.append(Library.versions_for_key_async(dep_key))
      for i, dep in enumerate(version.dependencies):
        parsed_dep = Dependency.fromString(dep)
        versions = version_futures[i].get_result()
        versions.reverse()
        while len(versions) > 0 and not versiontag.match(versions[0], parsed_dep.version):
          versions.pop()
        if len(versions) == 0:
          dependencies.append({
              'error': 'unsatisfyable dependency',
              'owner': parsed_dep.owner,
              'repo': parsed_dep.repo,
              'versionSpec': parsed_dep.version
          })
        else:
          dependencies.append(brief_metadata_from_datastore(parsed_dep.owner, parsed_dep.repo, versions[0]))
      result['dependencies'] = dependencies
    self.response.headers['Access-Control-Allow-Origin'] = '*'
    self.response.headers['Content-Type'] = 'application/json'
    self.response.write(json.dumps(result))

class GetHydroData(webapp2.RequestHandler):
  def get(self, owner, repo, ver=None):
    # TODO: Share all of this boilerplate between GetDataMeta and GetHydroData
    self.response.headers['Access-Control-Allow-Origin'] = '*'
    owner = owner.lower()
    repo = repo.lower()
    library_key = ndb.Key(Library, '%s/%s' % (owner, repo))
    # TODO: version shouldn't be optional here
    if ver is None:
      versions = Version.query(ancestor=library_key).map(lambda v: v.key.id())
      versions.sort(versiontag.compare)
      if versions == []:
        self.response.set_status(404)
        return
      ver = versions[-1]
    version_key = ndb.Key(Library, '%s/%s' % (owner, repo), Version, ver)
    hydro = Content.get_by_id('hydrolyzer', parent=version_key, read_policy=ndb.EVENTUAL_CONSISTENCY)
    if hydro is None:
      self.response.set_status(404)
      return

    self.response.headers['Content-Type'] = 'application/json'
    self.response.write(hydro.content)

class GetDependencies(webapp2.RequestHandler):
  def get(self, owner, repo, ver=None):
    self.response.headers['Access-Control-Allow-Origin'] = '*'

    owner = owner.lower()
    repo = repo.lower()
    version_key = ndb.Key(Library, '%s/%s' % (owner, repo), Version, ver)

    hydrolyzer = Content.get_by_id('hydrolyzer', parent=version_key, read_policy=ndb.EVENTUAL_CONSISTENCY)
    if hydrolyzer is None:
      self.response.set_status(404)
      return

    dependencies = json.loads(hydrolyzer.content).get('bowerDependencies', None)
    if dependencies is None:
      self.response.set_status(404)
      return

    self.response.headers['Content-Type'] = 'application/json'
    self.response.write(json.dumps(dependencies))

# pylint: disable=invalid-name
app = webapp2.WSGIApplication([
    webapp2.Route(r'/api/meta/<owner>/<repo>', handler=GetDataMeta),
    webapp2.Route(r'/api/meta/<owner>/<repo>/<ver>', handler=GetDataMeta),
    webapp2.Route(r'/api/docs/<owner>/<repo>', handler=GetHydroData),
    webapp2.Route(r'/api/docs/<owner>/<repo>/<ver>', handler=GetHydroData),
    webapp2.Route(r'/api/deps/<owner>/<repo>/<ver>', handler=GetDependencies),
    webapp2.Route(r'/api/search/<terms>', handler=SearchContents, name='search'),
], debug=True)
