#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
#  metadata_db_server.py
#  
#  Copyright 2022 Giorgio Gilestro <giorgio@gilest.ro>
#  
#  This program is free software; you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation; either version 2 of the License, or
#  (at your option) any later version.
#  
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#  
#  You should have received a copy of the GNU General Public License
#  along with this program; if not, write to the Free Software
#  Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston,
#  MA 02110-1301, USA.
#  
#  

import os
import logging
import optparse
import json
import bottle

from ast import literal_eval

try:
    from ethoscopy.metadata_db import db_organiser, metadata_handler, metadata_crawler
except:
    from metadata_db import db_organiser, metadata_handler, metadata_crawler

app = bottle.Bottle()
STATIC_DIR = "./static"

@app.route('/static/<filepath:path>')
def server_static(filepath):
    return bottle.static_file(filepath, root=STATIC_DIR)

@app.route('/')
def index():
    return bottle.static_file('index.html', root=STATIC_DIR)

@app.route('/save', method='POST')
def save():

    # text parameters that were received
    #for k in bottle.request.forms.keys(): print (k, bottle.request.forms.get(k)) 

    #file upload
    allowed_extensions = ['.csv', '.tsv']

    upload = bottle.request.files.get('file')
    if not upload:
        return {'success' : False, 'message' : 'A metadata file was not specified.'}

    name, ext = os.path.splitext(upload.filename)
    if ext not in allowed_extensions:
        return {'success' : False, 'message' : 'Extensions allowed are %s.' % " / ".join(allowed_extensions)}

    try:
        upload.save(meta_db.upload_folder, overwrite=True)

    except Exception as e:
        print (e)
        return {'success' : False, 'message' : str(e)}

    try:
        #After saving, creates the info file
        uploaded_metadata = metadata_handler (  filename = os.path.join (meta_db.upload_folder, upload.filename), 
                                                project = bottle.request.forms.get('project'),
                                                tags = literal_eval(bottle.request.forms.get('tags')),
                                                authors = literal_eval(bottle.request.forms.get('authors')),
                                                doi = bottle.request.forms.get('doi'),
                                                description = bottle.request.forms.get('description')
                                                )
        uploaded_metadata.save()

        meta_db.refresh_all_info()

        return {'success' : True, 'message' : 'Metadata file successfully uploaded.' }

    except Exception as e:
        return {'success' : False, 'message' : str(e)}


@app.route('/download', method='GET')
def download():
    hash_id = bottle.request.query.get('id', '')
    dwn_type = bottle.request.query.get('type', '')
    fullpath = meta_db.request(hash_id)['filename']
    path, filename = os.path.split( fullpath )
    
    if dwn_type == 'metadata':
        return bottle.static_file (filename, root=path,  download=filename)

    elif dwn_type == 'db_found':
        md = metadata_handler(fullpath)
        fn = os.path.splitext(filename)[0] + '.db_found.csv'
        md.list_dbs(notfound=False).to_csv(os.path.join ('/tmp/', fn))
        return bottle.static_file (fn, root='/tmp/',  download=fn)

    elif dwn_type == 'db_notfound':
        md = metadata_handler(fullpath)
        fn = os.path.splitext(filename)[0] + '.db_notfound.csv'
        md.list_dbs(notfound=True).to_csv(os.path.join ('/tmp/', fn))
        return bottle.static_file (fn, root='/tmp/',  download=fn)
        

@app.route('/refresh', method='GET')
def refresh():
    hash_id = bottle.request.query.get('id', '')
    filename = meta_db.request(hash_id)['filename']
    md = metadata_handler ( filename )
    md.associate_to_db(etho_db)
    md.save()
    return {'success' : True, 'message' : 'Metadata succefully associated.' }


@app.route('/info_tree', method='GET')
def get_metadata_tree():
    return meta_db.all_projects


@app.route('/metadata', method='GET')
def get_metadata():
    hash_id = bottle.request.query.get('id', '')
    return meta_db.request(hash_id)

@app.route('/known')
def available_options():
    return meta_db.available_options

def close(exit_status=0):
    logging.info("Closing server")
    os._exit(exit_status)
    
    
if __name__ == '__main__':
    logging.getLogger().setLevel(logging.INFO)
    parser = optparse.OptionParser()
    parser.add_option("-d", "--db", dest="db_path", default="/mnt/ethoscope_results", help="Path to the root of the ethoscope db files")
    parser.add_option("-f", "--metadata", dest="md_path", default="/opt/ethoscope_metadata", help="Path to the root of the metadata files")
    parser.add_option("-r", "--refresh", dest="refresh_db", default=False, help="Refresh ethoscope database on start", action="store_true")

    parser.add_option("-D", "--debug", dest="debug", default=False, help="Set DEBUG mode ON", action="store_true")
    parser.add_option("-p", "--port", dest="port", default=8081, help="port")

    (options, args) = parser.parse_args()

    option_dict = vars(options)
    DB_PATH = option_dict["db_path"]
    MD_PATH = option_dict["md_path"]
    REFRESH_DB = option_dict["refresh_db"]

    PORT = option_dict["port"]
    DEBUG = option_dict["debug"]

    if DEBUG:
        logging.basicConfig()
        logging.getLogger().setLevel(logging.DEBUG)
        logging.info("Logging using DEBUG SETTINGS")

    #DB_PATH points to the SQLlite db files. It can (should) be mounted as readonly as this never writes there
    #MD_PATH points to the metadata files. This will also contain the csv file with a summary of the SQLite dbs. Must be writable.

    etho_db = db_organiser(DB_PATH, refresh=REFRESH_DB, csv_path=MD_PATH)
    meta_db = metadata_crawler(path=MD_PATH)

    try:
        app.run(host='0.0.0.0', port=PORT, debug=DEBUG)

    except KeyboardInterrupt:
        logging.info("Stopping server cleanly")
        pass

    except socket.error as e:
        logging.error(traceback.format_exc())
        logging.error("Port %i is probably not accessible for you. Maybe use another one e.g.`-p 8000`" % PORT)

    except Exception as e:
        logging.error(traceback.format_exc())