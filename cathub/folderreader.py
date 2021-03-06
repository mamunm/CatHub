from .cathubsqlite import CathubSQLite
from .tools import get_bases, clear_prefactor, clear_state, get_pub_id, extract_atoms
from .ase_tools import collect_structures
from . import ase_tools

import sys
from datetime import date
import numpy as np
import os
import copy
import json
import yaml


class FolderReader:
    """
    Class for reading data from organized folders and writing to local
    CathubSQLite database. Folders should be arranged with
    make_folders_template and are read in the order:

    level:

    0    folder_name
    1    |-- publication
    2        |-- dft_code
    3            |-- dft_functional
    4                |-- gas
    4                |-- metal1
    5                    |-- facet
    6                        |-- reaction

    Parameters
    ----------
    foldername: str
    debug: bool
        default is False. Choose True if the folderreader should  continue
        in spite of errors.
    update: bool
        Update data if allready present in database file. defalt is True
    energy_limit: float
        Limit for acceptable absolute reaction energies
    """

    def __init__(self, folder_name, debug=False, strict=True, verbose=False,
                 update=True, energy_limit=5, stdin=sys.stdin,
                 stdout=sys.stdout, userhandle=None):
        self.debug = debug
        self.strict = strict
        self.verbose = verbose
        self.update = update
        self.energy_limit = energy_limit

        self.data_base, self.user, self.user_base \
            = get_bases(folder_name=folder_name)
        if userhandle:
            self.user = userhandle
        self.user_base_level = len(self.user_base.split("/"))

        self.pub_level = 1
        self.DFT_level = 2
        self.XC_level = 3
        self.reference_level = 4
        self.slab_level = 5
        self.reaction_level = 6
        self.final_level = 6

        self.stdin = stdin
        self.stdout = stdout

        self.cathub_db = None
        self.coverages = None
        self.omit_folders = []
        self.doi = None
        self.title = None
        self.authors = None
        self.year = None
        self.tags = None
        self.pub_id = None
        self.warnings = []

    def read(self, skip=[], goto_metal=None, goto_reaction=None):
        """
        Get reactions from folders.

        Parameters
        ----------
        skip: list of str
            list of folders not to read
        goto_reaction: str
            Skip ahead to this metal
        goto_reaction:
            Skip ahead to this reacion
        """
        if len(skip) > 0:
            for skip_f in skip:
                self.omit_folders.append(skip_f)

        """ If publication level is input"""
        if os.path.isfile(self.data_base + '/publication.txt'):
            self.user_base_level -= 1

        self.stdout.write('---------------------- \n')
        self.stdout.write('Starting folderreader! \n')
        self.stdout.write('---------------------- \n')
        found_reaction = False
        for root, dirs, files in os.walk(self.user_base):
            for omit_folder in self.omit_folders:  # user specified omit_folder
                if omit_folder in dirs:
                    dirs.remove(omit_folder)
            level = len(root.split("/")) - self.user_base_level

            if level == self.pub_level:
                self.read_pub(root)

            if level == self.DFT_level:
                self.DFT_code = os.path.basename(root)

            if level == self.XC_level:
                self.DFT_functional = os.path.basename(root)
                self.gas_folder = root + '/gas/'
                self.read_gas()

            if level == self.reference_level:
                if 'gas' in os.path.basename(root):
                    continue

                if goto_metal is not None:
                    if os.path.basename(root) == goto_metal:
                        goto_metal = None
                    else:
                        dirs[:] = []  # don't read any sub_dirs
                        continue
                self.read_bulk(root)

            if level == self.slab_level:
                self.read_slab(root)

            if level == self.reaction_level:
                if goto_reaction is not None:
                    if os.path.basename(root) == goto_reaction:
                        goto_reaction = None
                    else:
                        dirs[:] = []  # don't read any sub_dirs
                        continue

                self.read_reaction(root)

            if level == self.final_level:
                self.root = root
                self.read_energies(root)
                if self.key_value_pairs_reaction is not None:
                    yield self.key_value_pairs_reaction

    def write(self, skip=[], goto_reaction=None):
        for key_values in self.read(skip=skip, goto_reaction=goto_reaction):
            with CathubSQLite(self.cathub_db) as db:
                id = db.check(
                    key_values['chemical_composition'],
                    key_values['reaction_energy'])
                if id is None:
                    try:
                        id = db.write(key_values)
                        self.stdout.write(
                            '  Written to reaction db row id = {}\n'.format(id))
                    except BaseException as e:
                        self.raise_error(
                            'Writing to db: {}. {}'.format(e, self.root))

                elif self.update:
                    db.update(id, key_values)
                    self.stdout.write(
                        '  Updated reaction db row id = {}\n'.format(id))
                else:
                    self.stdout.write(
                        '  Already in reaction db with row id = {}\n'.format(id))
        assert self.cathub_db is not None, \
            'Wrong folder! No reactions found in {base}'\
            .format(base=self.user_base)
        self.print_warnings()
        self.get_summary()

    def get_summary(self):
        with CathubSQLite(self.cathub_db) as db:
            db.print_summary()

    def write_publication(self, pub_data):
        with CathubSQLite(self.cathub_db) as db:
            pid = db.check_publication(self.pub_id)
            if pid is None:
                pid = db.write_publication(pub_data)
                self.stdout.write(
                    'Written to publications db row id = {}\n'.format(pid))
        return pid

    def read_pub(self, root):
        pub_folder = os.path.basename(root)
        publication_keys = {}
        try:
            with open(root + '/publication.txt', 'r') as f:
                pub_data = yaml.load(f)
            if 'url' in pub_data.keys():
                del pub_data['url']
            self.title = pub_data['title']
            self.authors = pub_data['authors']
            self.year = pub_data['year']
            if 'doi' not in pub_data:
                pub_data.update({'doi': None})
                self.stdout.write('ERROR: No doi\n')
            else:
                self.doi = pub_data['doi']
            if 'tags' not in pub_data:
                pub_data.update({'tags': None})
                self.stdout.write('ERROR: No tags\n')

            for key, value in pub_data.items():
                if isinstance(value, list):
                    value = json.dumps(value)
                else:
                    try:
                        value = int(value)
                    except BaseException:
                        pass

        except Exception as e:
            self.stdout.write(
                'ERROR: insufficient publication info {e}\n'.format(
                    **locals()))
            pub_data = {'title': None,
                        'authors': None,
                        'journal': None,
                        'volume': None,
                        'number': None,
                        'pages': None,
                        'year': None,
                        'publisher': None,
                        'doi': None,
                        'tags': None
                        }

        try:
            with open(root + '/energy_corrections.txt', 'r') as f:
                self.energy_corrections = yaml.load(f)
                if self.energy_corrections:
                    self.stdout.write('----------------------------\n')
                    self.stdout.write('Applying energy corrections:\n')
                    for key, value in self.energy_corrections.items():
                        if value > 0:
                            sgn = '+'
                        else:
                            sgn = '-'
                            sys.stdout.write('  {key}: {sgn}{value}\n'
                                             .format(key=key, sgn=sgn,
                                                     value=value))
                    self.stdout.write('----------------------------\n')
                else:
                    self.energy_corrections = {}

        except BaseException:
            self.energy_corrections = {}

        if pub_data['title'] is None:
            self.title = os.path.basename(root)
            pub_data.update({'title': self.title})
        if pub_data['authors'] is None:
            self.authors = [self.user]
            pub_data.update({'authors': self.authors})
        if pub_data['year'] is None:
            self.year = date.today().year
            pub_data.update({'year': self.year})

        self.pub_id = get_pub_id(self.title, self.authors, self.year)
        self.cathub_db = '{}{}.db'.format(self.data_base, self.pub_id)
        self.stdout.write(
            'Writing to .db file {}:\n \n'.format(self.cathub_db))
        pub_data.update({'pub_id': self.pub_id})
        pid = self.write_publication(pub_data)

    def read_gas(self):
        gas_structures = collect_structures(self.gas_folder)
        self.ase_ids_gas = {}
        self.gas = {}

        for gas in gas_structures:
            ase_id = None
            found = False

            chemical_composition = \
                ''.join(sorted(ase_tools.get_chemical_formula(
                    gas, mode='all')))
            chemical_composition_hill = ase_tools.get_chemical_formula(
                gas, mode='hill')
            energy = gas.get_potential_energy()
            key_value_pairs = {"name": chemical_composition_hill,
                               'state': 'gas',
                               'epot': energy}

            id, ase_id = ase_tools.check_in_ase(
                gas, self.cathub_db)

            if ase_id is None:
                ase_id = ase_tools.write_ase(gas, self.cathub_db,
                                             self.stdout,
                                             self.user,
                                             **key_value_pairs)
            elif self.update:
                ase_tools.update_ase(self.cathub_db, id,
                                     self.stdout, **key_value_pairs)

            self.ase_ids_gas.update({chemical_composition: ase_id})
            self.gas.update({chemical_composition: gas})

    def read_bulk(self, root):
        basename = os.path.basename(root)
        assert '_' in basename, \
            """Wrong folderstructure! Folder should be of format
            <metal>_<crystalstructure> but found {basename}""".format(
                basename=basename
            )
        self.metal, self.crystal = basename.split('_', 1)

        self.stdout.write(
            '------------------------------------------------------\n')
        self.stdout.write(
            '                    Surface:  {}\n'.format(self.metal))
        self.stdout.write(
            '------------------------------------------------------\n')

        self.ase_ids = {}

        bulk_structures = collect_structures(root)
        n_bulk = len(bulk_structures)
        if n_bulk == 0:
            return
        elif n_bulk > 1:
            self.raise_warning('More than one bulk structure submitted at {root}'
                               .format(root=root))
            return

        bulk = bulk_structures[0]
        ase_id = None
        energy = ase_tools.get_energies([bulk])

        key_value_pairs = {"name": self.metal,
                           'state': 'bulk',
                           'epot': energy}

        id, ase_id = ase_tools.check_in_ase(
            bulk, self.cathub_db)
        if ase_id is None:
            ase_id = ase_tools.write_ase(bulk, self.cathub_db, self.stdout,
                                         self.user, **key_value_pairs)
        elif self.update:
            ase_tools.update_ase(self.cathub_db, id,
                                 self.stdout, **key_value_pairs)

        self.ase_ids.update({'bulk' + self.crystal: ase_id})

    def read_slab(self, root):
        self.facet = root.split('/')[-1].split('_')[0]
        self.ase_facet = 'x'.join(list(self.facet))

        empty_structures = collect_structures(root)
        n_empty = len(empty_structures)

        if n_empty == 0:
            self.raise_warning('No empty slab submitted at {root}'
                               .format(root=root))
            self.empty = None
            return
        elif n_empty > 1:
            self.raise_warning('More than one empty slab submitted at {root}'
                               .format(root=root))
            filename_collapse = ''.join([empty.info['filename']
                                         for empty in empty_structures])
            if 'TS' not in filename_collapse:
                return

        self.empty = empty_structures[0]

        ase_id = None
        energy = ase_tools.get_energies([self.empty])
        key_value_pairs = {"name": self.metal,
                           'state': 'star',
                           'epot': energy}

        key_value_pairs.update({'species': ''})

        id, ase_id = ase_tools.check_in_ase(
            self.empty, self.cathub_db)

        if ase_id is None:
            ase_id = ase_tools.write_ase(self.empty, self.cathub_db, self.stdout,
                                         self.user, **key_value_pairs)
        elif self.update:
            ase_tools.update_ase(self.cathub_db, id,
                                 self.stdout, **key_value_pairs)
        self.ase_ids.update({'star': ase_id})

    def read_reaction(self, root):
        folder_name = os.path.basename(root)

        self.reaction, self.sites = ase_tools.get_reaction_from_folder(
            folder_name)  # reaction dict

        self.stdout.write(
            '----------- REACTION:  {} --> {} --------------\n'
            .format('+'.join(self.reaction['reactants']),
                    '+'.join(self.reaction['products'])))

        self.reaction_atoms, self.prefactors, self.prefactors_TS, \
            self.states = ase_tools.get_reaction_atoms(self.reaction)

        """Create empty dictionaries"""
        r_empty = ['' for n in range(len(self.reaction_atoms['reactants']))]
        p_empty = ['' for n in range(len(self.reaction_atoms['products']))]
        self.structures = {'reactants': r_empty[:],
                           'products': p_empty[:]}

        key_value_pairs = {}

        """ Match reaction gas species with their atomic structure """
        for key, mollist in self.reaction_atoms.items():
            for i, molecule in enumerate(mollist):
                if self.states[key][i] == 'gas':
                    assert molecule in self.ase_ids_gas.keys(), \
                        """Molecule {molecule} is missing in folder {gas_folder}"""\
                        .format(molecule=clear_prefactor(self.reaction[key][i]),
                                gas_folder=self.gas_folder)
                    self.structures[key][i] = self.gas[molecule]
                    species = clear_prefactor(
                        self.reaction[key][i])
                    key_value_pairs.update(
                        {'species': clear_state(species)})
                    self.ase_ids.update({species: self.ase_ids_gas[molecule]})

        """ Add empty slab to structure dict"""
        for key, mollist in self.reaction_atoms.items():
            if '' in mollist:
                n = mollist.index('')
                self.structures[key][n] = self.empty

    def read_energies(self, root):
        self.key_value_pairs_reaction = None

        slab_structures = collect_structures(root)

        if len(slab_structures) == 0:
            self.raise_warning('No structure files in {root}: Skipping this folder'
                               .format(root=root))
            return

        # Remove old species from ase_ids
        all_reaction_species = list(self.reaction.values())[0] \
            + list(self.reaction.values())[1]
        all_reaction_species = [clear_prefactor(rs) for rs in
                                all_reaction_species]

        for ase_id in list(self.ase_ids.keys()):
            if ase_id == 'star' or 'bulk' in ase_id:
                continue
            if not ase_id in all_reaction_species:
                del self.ase_ids[ase_id]

        if 'TS' in self.structures:  #Delete old TS
            del self.structures['TS']
            del self.structures['TSempty']
            del self.prefactors['TS']

        n_atoms = np.array([])
        ts_i = None
        tsempty_i = None
        chemical_composition_slabs = []
        breakloop = False
        for i, slab in enumerate(slab_structures):
            f = slab.info['filename']
            if 'empty' in f and 'TS' in f:
                tsempty_i = i
            elif 'TS' in f:
                ts_i = i
            chemical_composition_slabs = \
                np.append(chemical_composition_slabs,
                          ase_tools.get_chemical_formula(slab, mode='all'))
            n_atoms = np.append(n_atoms, len(slab))

        empty = self.empty
        if not empty:
            reactant_entries = self.reaction['reactants'] + \
                self.reaction['products']
            if 'star' in reactant_entries:
                message = 'Empty slab needed for reaction!'
                self.raise_error(message)
                return
            else:
                empty = slab_structures[0]
                self.raise_warning("Using '{}' as a reference instead of empty slab"
                                   .format(empty.info['filename']))
        empty_atn = list(empty.get_atomic_numbers())

        prefactor_scale = copy.deepcopy(self.prefactors)
        for key1, values in prefactor_scale.items():
            prefactor_scale[key1] = [1 for v in values]

        key_value_pairs = {}

        key_value_pairs.update({'name':
                                ase_tools.get_chemical_formula(empty),
                                'facet': self.ase_facet,
                                'state': 'star'})

        """ Match adsorbate structures with reaction entries"""
        for i, slab in enumerate(slab_structures):
            f = slab.info['filename']
            atns = list(slab.get_atomic_numbers())
            if not (np.array(atns) > 8).any() and \
               (np.array(empty_atn) > 8).any():
                self.raise_warning("Only molecular species for structure: {}"
                                   .format(f))
                continue

            """Get supercell size relative to empty slab"""
            supercell_factor = 1
            if len(atns) > len(empty_atn) * 2:  # different supercells
                supercell_factor = len(res_slab_atn) // len(empty_atn)

            """Atomic numbers of adsorbate"""
            ads_atn = copy.copy(atns)
            for atn in empty_atn * supercell_factor:
                ads_atn.remove(atn)
            ads_atn = sorted(ads_atn)
            if ads_atn == [] and 'star' in self.ase_ids:
                self.raise_warning("No adsorbates for structure: {}"
                                   .format(f))
                continue

            ase_id = None
            id, ase_id = ase_tools.check_in_ase(slab, self.cathub_db)
            key_value_pairs.update({'epot': ase_tools.get_energies([slab])})

            if i == ts_i:  # transition state
                self.structures.update({'TS': [slab]})
                self.prefactors.update({'TS': [1]})
                prefactor_scale.update({'TS': [1]})
                key_value_pairs.update({'species': 'TS'})
                if ase_id is None:
                    ase_id = ase_tools.write_ase(slab, self.cathub_db,
                                                 self.stdout,
                                                 self.user,
                                                 **key_value_pairs)
                elif self.update:
                    ase_tools.update_ase(self.cathub_db, id, self.stdout,
                                         **key_value_pairs)
                self.ase_ids.update({'TSstar': ase_id})
                continue

            if i == tsempty_i:  # empty slab for transition state
                self.structures.update({'TSempty': [slab]})
                self.prefactors.update({'TSempty': [1]})
                prefactor_scale.update({'TSempty': [1]})
                key_value_pairs.update({'species': ''})
                if ase_id is None:
                    ase_id = ase_tools.write_ase(slab, self.cathub_db,
                                                 self.stdout,
                                                 self.user,
                                                 **key_value_pairs)
                elif self.update:
                    ase_tools.update_ase(self.cathub_db, id, self.stdout,
                                         **key_value_pairs)
                self.ase_ids.update({'TSemptystar': ase_id})
                continue

            found = False
            for key, mollist in self.reaction.items():
                if found:
                    break
                for n, molecule in enumerate(mollist):
                    if found:
                        break
                    if not self.states[key][n] == 'star':  # only slab stuff
                        continue
                    if not self.structures[key][n] == '':  # allready found
                        continue
                    molecule = clear_state(clear_prefactor(molecule))
                    if molecule == '':
                        continue
                    molecule_atn = sorted(
                        ase_tools.get_numbers_from_formula(molecule))
                    if slab.info['filename'] == molecule:
                        if ads_atn == molecule_atn:
                            found = True
                            match_key = key
                            match_n = n
                            break
                        else:
                            raise_warning('Name of file does not match chemimcal formula: {}'
                                          .format(slab.info['filename']))

                    for n_ads in range(1, 5):
                        mol_atn = sorted(molecule_atn * n_ads)
                        if (ads_atn == mol_atn or len(ads_atn) == 0):
                            match_n_ads = n_ads
                            found = True
                            match_key = key
                            match_n = n
                            break
                """
                if not found:
                    adsmollist = [clear_state(clear_prefactor(mol))
                                  for i, mol in enumerate(mollist)
                                  if self.states[key][i] == 'star'
                                  and not clear_prefactor(self.reaction[key][i]) == 'star']
                    adsmolidx = [i for i, mol in enumerate(mollist)
                                 if self.states[key][i] == 'star'
                                 and not clear_prefactor(self.reaction[key][i]) == 'star']
                    adsmol = ''.join(adsmollist)
                    molecule_atn = sorted(ase_tools.get_numbers_from_formula(adsmol))
                    if ads_atn == molecule_atn:
                        #print(self.structures)
                        #print(self.reaction)
                        #print(self.reaction_atoms)
                        #print(self.states)
                        #print(self.energy_corrections)
                        reaction_entry = ''
                        new_reaction = ''
                        reaction_atom = extract_atoms(adsmol)
                        state = 'star'
                        energy_correction = 0
                        for idx in adsmolidx:
                            r_entry = clear_state(self.reaction[key][idx])
                            reaction_entry += r_entry + '_'
                            if self.energy_corrections.get(r_entry, None):
                                energy_corrextion += self.energy_corrections[r_entry]
                    #        del self.structures[key][i]
                    #        del self.reaction[key][i],
                    #        self.reaction_atoms[key][i],
                    #        self.states[key][i],
                    #        self.energy_corrections
                """
            if found:
                key = match_key
                n = match_n
                n_ads = match_n_ads
                self.structures[key][n] = slab
                species = clear_prefactor(
                    self.reaction[key][n])
                id, ase_id = ase_tools.check_in_ase(
                    slab, self.cathub_db)
                key_value_pairs.update(
                    {'species':
                     clear_state(
                         species),
                     'n': n_ads,
                     'site': str(self.sites.get(species, ''))})
                if ase_id is None:
                    ase_id = ase_tools.write_ase(
                        slab, self.cathub_db, self.stdout,
                        self.user,
                        **key_value_pairs)
                elif self.update:
                    ase_tools.update_ase(
                        self.cathub_db, id, self.stdout,
                        **key_value_pairs)
                self.ase_ids.update({species: ase_id})

            if n_ads > 1:
                for key1, values in prefactor_scale.items():
                    for mol_i in range(len(values)):
                        if self.states[key1][mol_i] == 'gas':
                            prefactor_scale[key1][mol_i] = n_ads

            if supercell_factor > 1:
                for key2, values in prefactor_scale.items():
                    for mol_i in range(len(values)):
                        if self.reaction[key2][mol_i] == 'star':
                            prefactor_scale[key2][mol_i] *= supercell_factor

        # Check that all structures have been found
        for key, structurelist in self.structures.items():
            if '' in structurelist:
                index = structurelist.index('')
                molecule = clear_state(
                    clear_prefactor(self.reaction[key][index]))
                if self.states[key][index] == 'star':
                    message = "Adsorbate '{}' not found for any structure files in '{}'."\
                        .format(molecule, root) + \
                        " Please check your adsorbate structures and the empty slab."
                if self.states[key][index] == 'gas':
                    message = "Gas phase molecule '{}' not found for any structure files in '{}'."\
                        .format(molecule, self.gas_folder) + \
                        " Please check your gas phase references."
                self.raise_error(message)
                return

        surface_composition = self.metal
        chemical_composition = ase_tools.get_chemical_formula(empty)

        prefactors_final = copy.deepcopy(self.prefactors)
        for key in self.prefactors:
            for i, v in enumerate(self.prefactors[key]):
                prefactors_final[key][i] = self.prefactors[key][i] * \
                    prefactor_scale[key][i]

        reaction_energy = None
        activation_energy = None
        try:
            reaction_energy, activation_energy = \
                ase_tools.get_reaction_energy(
                    self.structures, self.reaction,
                    self.reaction_atoms,
                    self.states, prefactors_final,
                    self.prefactors_TS,
                    self.energy_corrections)
        except BaseException as e:
            message = "reaction energy failed for files in '{}'".format(root)
            self.raise_error(message + '\n' + str(e))

        if not -self.energy_limit < reaction_energy < self.energy_limit:
            self.raise_error('reaction energy is very large: {} eV \n  '\
                             .format(reaction_energy) +
                             'Folder: {}. \n  '.format(root) +
                             'If the value is correct, you can reset the limit with cathub folder2db --energy-limit <value>. Default is --energy-limit=5 (eV)'
                             )
        if activation_energy is not None:
            if activation_energy < reaction_energy:
                self.raise_warning('activation energy is smaller than reaction energy: {} vs {} eV \n  Folder: {}'.format(activation_energy, reaction_energy, root))
            if not activation_energy < self.energy_limit:
                self.raise_error(' Very large activation energy: {} eV \n  Folder: {}'
                                 .format(activation_energy, root))

        reaction_info = {'reactants': {},
                         'products': {}}

        for key in ['reactants', 'products']:
            for i, r in enumerate(self.reaction[key]):
                r = clear_prefactor(r)
                reaction_info[key].update({r: self.prefactors[key][i]})

        self.key_value_pairs_reaction = {
            'chemical_composition': chemical_composition,
            'surface_composition': surface_composition,
            'facet': self.facet,
            'sites': self.sites,
            'coverages': self.coverages,
            'reactants': reaction_info['reactants'],
            'products': reaction_info['products'],
            'reaction_energy': float(reaction_energy),
            'activation_energy': activation_energy,
            'dft_code': self.DFT_code,
            'dft_functional': self.DFT_functional,
            'pub_id': self.pub_id,
            'doi': self.doi,
            'year': int(self.year),
            'ase_ids': self.ase_ids,
            'energy_corrections': self.energy_corrections,
            'username': self.user}

    def raise_error(self, message):
        if self.debug:
            self.stdout.write('--------------------------------------\n')
            self.stdout.write('Error: ' + message + '\n')
            self.stdout.write('--------------------------------------\n')
            self.warnings.append('Error: ' + message)
        else:
            self.print_warnings()
            raise RuntimeError(message)

    def raise_warning(self, message):
        self.stdout.write('Warning: ' + message + '\n')
        self.warnings.append('Warning: ' + message)

    def print_warnings(self):
        self.stdout.write('-------------------------------------------\n')
        self.stdout.write('All errors and warnings: ' + '\n')
        for warning in self.warnings:
            self.stdout.write('    ' + warning + '\n')
        self.stdout.write('-------------------------------------------\n')
