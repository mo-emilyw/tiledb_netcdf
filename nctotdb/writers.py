import os

import numpy as np
import tiledb
import zarr


class TDBWriter(object):
    """
    Provides a class to write Python objects loaded from NetCDF to TileDB.
    
    Data Model: an instance of `NCDataModel` supplying data from a NetCDF file.
    Filepath: the filepath to save the tiledb array at.
    
    """
    def __init__(self, data_model, tiledb_filepath,
                 tiledb_array_name=None, unlimited_dims=None):
        self.data_model = data_model
        self.tiledb_filepath = tiledb_filepath
        self.unlimited_dims = unlimited_dims
        
        self._tiledb_array_name = tiledb_array_name
        
        if self._tiledb_array_name is None:
            self.array_name = os.path.basename(os.path.splitext(self.data_model.netcdf_filename)[0])
        else:
            self.array_name = self._tiledb_array_name
    
    def _public_domain_name(self, domain):
        domain_index = self.data_model.domains.index(domain)
        return f'domain_{domain_index}'
    
    def _create_tdb_directory(self, group_dirname):
        """
        Create an on-filesystem directory for a tiledb group if it does
        not exist, and ignore the error if it does.
        
        """
        try:
            os.makedirs(group_dirname)
        except FileExistsError:
            pass
    
    def _create_tdb_dim(self, dim_name):
        dim_coord = self.data_model._ncds_vars[dim_name]
        chunks = self.data_model.get_chunks(dim_name)
        
        # TODO: work out nD coords (although a DimCoord will never be nD).
        dim_coord_len, = dim_coord.shape
        
        # Set the tdb dimension dtype to `int64` regardless of input.
        # All tdb dims in a domain must have exactly the same dtype.
        dim_dtype = np.int64
        
        # Sort out the domain, based on whether the dim is unlimited,
        # or whether it was specified that it should be by `self.unlimited_dims`.
        if dim_name in self.unlimited_dims:
            domain_max = np.iinfo(dim_dtype).max - dim_coord_len
        elif dim_name in self.data_model.unlimited_dim_coords:
            domain_max = np.iinfo(dim_dtype).max - dim_coord_len
        else:
            domain_max = dim_coord_len

        return tiledb.Dim(name=dim_name,
                          domain=(0, domain_max),
                          tile=chunks,
                          dtype=dim_dtype)

    def _create_tdb_attrs(self, data_vars):
        # Create array attribute.
        tdb_attrs = []
        for phenom_name in data_vars:
            data_var = self.data_model._ncds_vars[phenom_name]
            phenom = tiledb.Attr(name=phenom_name, dtype=data_var.dtype)
            tdb_attrs.append(phenom)
        return tdb_attrs
    
    def create_domain_arrays(self, domain_vars, group_dirname):
        # Create one single-attribute array per data var in this NC domain.
        
        for var_name in domain_vars:
            # Set dims for the enclosing domain.
            data_var = self.data_model._ncds_vars[var_name]
            data_var_dims = data_var.dimensions
            array_dims = [self._create_tdb_dim(dim_name) for dim_name in data_var_dims]
            tdb_domain = tiledb.Domain(*array_dims)

            # Get tdb attributes.
            attr = tiledb.Attr(name=var_name, dtype=data_var.dtype)

            # Create the URI for the array.
            array_filename = os.path.join(group_dirname, var_name)
            # Create an empty array.
            schema = tiledb.ArraySchema(domain=tdb_domain, sparse=False, attrs=[attr])
            tiledb.Array.create(array_filename, schema)

    def _array_indices(self, data_var, start_index):
        """Set the array indices to write the array data into."""
        shape = data_var.shape
        if isinstance(start_index, int):
            start_index = [start_index] * len(shape)
        
        array_indices = []
        for dim, start_ind in zip(shape, start_index):
            array_indices.append(slice(start_ind, dim+start_ind))
        return tuple(array_indices)
    
    def populate_array(self, var_name, data_var, group_dirname,
                       start_index=0, write_meta=True):
        """
        Write the contents of a netcdf data variable into a tiledb array.
        
        """
        # Get the data variable and the filename of the array to write to.
        var_name = data_var.name
        array_filename = os.path.join(group_dirname, var_name)
        
        # Write to the array.
        with tiledb.open(array_filename, 'w') as A:
            # Write netcdf data var contents into array.
            write_indices = self._array_indices(data_var, start_index)
            A[write_indices] = data_var[...]

            if write_meta:
                # Set tiledb metadata from data var ncattrs.
                for ncattr in data_var.ncattrs():
                    A.meta[ncattr] = data_var.getncattr(ncattr)
    
    def populate_domain_arrays(self, domain_vars, group_dirname):
        """Populate all arrays with data from netcdf data vars within a tiledb group."""
        for var_name in domain_vars:
            data_var = self.data_model._ncds_vars[var_name]
            self.populate_array(var_name, data_var, group_dirname)

    def create_domains(self):
        """
        We need to create one TDB group per data variable in the data model,
        organised by domain. 
        
        """
        for domain in self.data_model.domains:
            # Get the variables in this netcdf super-domain.
            domain_vars = self.data_model.domain_varname_mapping[domain]
            
            # Create group.
            domain_name = self._public_domain_name(domain)
            group_dirname = os.path.join(self.tiledb_filepath, self.array_name, domain_name)
            # TODO: why is this necessary? Shouldn't tiledb create if this dir does not exist?
            self._create_tdb_directory(group_dirname)
            tiledb.group_create(group_dirname)
            
            # Get data vars in this domain and create an array for the domain.
            self.create_domain_arrays(domain_vars, group_dirname)
            
            # Populate this domain's array.
            self.populate_domain_arrays(domain_vars, group_dirname)
    
    def append(self, other_data_model, var_name, append_dim):
        """
        Append the data from a data variable in `other_data_model`
        by extending one dimension of that data variable in the tiledb
        described by `self`.
        
        Notes:
          * extends one dimension only on a single data variable
          * cannot create new dimensions, only extend existing dimensions
          
        Assumptions:
          * for now, that the data in other directly follows on from the
            data in self, so that there are no gaps or overlaps in the
            appended data
        
        """        
        # Sanity checks: is the var name in both self, other, and the tiledb?
        assert var_name in self.data_model.data_var_names
        assert var_name in other_data_model.data_var_names
        
        # And is the append dimension valid?
        self_data_var = self.data_model._ncds_vars[var_name]
        other_data_var = other_data_model._ncds_vars[var_name]
        assert append_dim in self_data_var.dimensions
        assert append_dim in other_data_var.dimensions
        assert self_data_var.dimensions == other_data_var.dimensions
        
        # Get domain for var_name and tiledb array path.
        domain = self.data_model.varname_domain_mapping[var_name]
        domain_name = self._public_domain_name(domain)
        domain_path = os.path.join(self.tiledb_filepath, self.array_name, domain_name)
        
        # Get the index for the append dimension.
        if not isinstance(append_dim, int):
            append_dim = self_data_var.dimensions.index(append_dim)
        
        # Get the offset along the append dimension, assuming that self and other are
        # contiguous along this dimension.
        append_dim_offset = self_data_var.shape[append_dim]
        offsets = [0] * len(self_data_var.shape)
        offsets[append_dim] = append_dim_offset
        
        # And append the data.
        self.populate_array(var_name, other_data_var, domain_path,
                            start_index=offsets, write_meta=False)

                
class ZarrWriter(object):
    """
    Provides a class to write Python objects loaded from NetCDF to zarr.
    
    TODO:
      * Support groups
      * Labelled dimensions / support for coords.
    
    """
    def __init__(self, data_model, filepath, group_name=None):
        self.data_model = data_model
        self.filepath = filepath
        self._group_name = group_name
        
        if self._group_name is None:
            self.group_name = os.path.basename(os.path.splitext(self.data_model.netcdf_filename)[0])
        else:
            self.group_name = self.group_name
        self.array_filename = f'{os.path.join(os.path.abspath("."), self.filepath, self.group_name)}.zarr'
        print(self.array_filename)
        
        self.group = None
        self.zarray = None
    
#     def create_array(self):
#         self.zarray = zarr.open(self.array_filename,
#                                 shape=self.data_model.shape,
#                                 mode='a',
#                                 chunks=self.data_model.chunks)

    def create_variable_datasets(self, var_names):
        """
        Create a zarr group - containing data variables and dimensions - for
        a given domain.
        
        A domain is described by the tuple of dimensions that describe it. 
        
        """
        
        # Write domain variables and dimensions into group.
        for var_name in var_names:
            nc_data_var = self.data_model._ncds_vars[var_name]
            chunks = self.data_model.get_chunks(var_name)
            data_array = self.group.create_dataset(var_name,
                                                     shape=nc_data_var.shape,
                                                     chunks=chunks,
                                                     dtype=nc_data_var.dtype)
            data_array[:] = nc_data_var[...]
            
            # Set array attributes from ncattrs.
            for ncattr in nc_data_var.ncattrs():
                data_array.attrs[ncattr] = nc_data_var.getncattr(ncattr)
                
            # Set attribute to specify var's dimensions.
            data_array.attrs['_ARRAY_DIMENSIONS'] = nc_data_var.dimensions
    
    def create_zarr(self):
        """
        Create a zarr for the contents of `self.data_model`. The grouped
        structure of this zarr is:
        
            root (filename)
             | - phenom_0
             | - phenom_1
             | - ...
             | - dimension_0
             | - dimension_1
             | - ...
             | - phenom_n
             | - ...
             
        TODO: add global NetCDF attributes to outermost zarr structure?
        
        """
        store = zarr.DirectoryStore(self.array_filename)
        self.group = zarr.group(store=store)
        
        # Write zarr datasets for data variables.
        for domain in self.data_model.domains:
            domain_vars = self.data_model.domain_varname_mapping[domain]
            self.create_variable_datasets(domain_vars)
            
        # Write zarr datasets for dimension variables.
        keys = [k for k in self.data_model.domain_varname_mapping]
        unique_flat_keys = set([k for domain in keys for k in domain])
        self.create_variable_datasets(unique_flat_keys)
        
    def append(self, other_data_model, group_name=None):
        """
        Append the contents of other onto self.group, optionally specifying
        a single zarr array to append to with `group_name`. 
        
        If this is not specified, extend all of the arrays in self.group_name
        with all of the arrays found in other. This assumes that the data
        variables in self and other:
          a) are identical
          b) all append along the same dimension.
          
        Note: append axis is limited to a single axis.
        
        """
        # Check names line up.
        if group_name is not None:
            assert group_name in self.data_model.data_var_names
        else:
            assert self.data_model.data_var_names == other_data_model.data_var_names
        
        # Work out the append axis.
        
        # Run the append.
        group_names = other_data_model.data_var_names if group_name is None else [group_name]
        for group_name in group_names:
            self_array = getattr(self.group, group_name)
            other_var = other_data_model._ncds_vars[group_name]
            axis = 0
            self_array.append(other_var[...], axis=axis)
            