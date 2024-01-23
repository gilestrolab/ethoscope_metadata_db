# Ethoscope metadata db
This is a package that can be used to organise your ethoscope database.
The core of the package is ethoscope_metadata_db which takes care of 
 - building an internal representation of the ethoscope data
 - handle metadata file associating them to experiments
 - collect information regarding a metadata file and its db dependencies
 - export that information

 A typical use case is to recover all the ethoscope dbs associated to a certain metadata file so that the user can collate them and upload them to a public dataset repository, such as Zenodo.

For instance:
```
from ethoscope_metadata_db import db_organiser, metadata_handler

metadata_filename = 'all_metadata.csv'
datapath = "/mnt/ethoscope_results"

#refresh the content of the ethoscope dbs
db = db_organiser(datapath, refresh=True, csv_path="/opt/ethoscope_metadata/")

#find the relevant files associated to our metadata - this will take a few seconds
meta = metadata_handler(metadata_filename, project='Joyce_2024', authors=['Joyce', 'Blackhurst', 'Falconio', 'French'] )
meta.associate_to_db(db)

#Give me a summary of what you found. Useful to know the size of the whole dataset.
meta.summary

#The followind dbs were not found on our machine
#This usually indicates that the ethoscope had failed but was not removed from the metadata
meta.list_dbs(notfound=True)

#Save to a txt file only the databases we have actually found
meta.export('Joyce_2024_only_dbs.txt')

#Saves all the info for our convenience
meta.export('Joyce_2024_only_dbs.txt', verbose=True)
```

The second class is the **metadata_db_server** which runs a web based UI
Instructions on to set it up and run it can be found [here](https://www.notion.so/giorgiogilestro/The-Metadata-Database-Server-cafac3d16ca04c0b8d4364e7624d7151).

## Installation
Install with pip

```
#optional pyenv
python -m venv ~/python-envs/ethoscope_db/
source ~/python-envs/ethoscope_db/bin/activate

#install in edit mode
pip install -e .
```
