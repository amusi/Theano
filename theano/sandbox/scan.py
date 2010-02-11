"""Provide Scan and related functions


 Scanning a function over sequential input(s) producing sequential output(s).

 Scanning is a general form of recurrence, which can be used for looping.

 The idea is that you 'scan' a function along some input sequence, producing 
 an output at each time-step that can be seen (but not modified) by the 
 function at the next time-step. (Technically, the function can see the 
 previous K  time-steps of your outputs and L time steps of your inputs, 
 future and past.)

 So for example, ``sum()`` could be computed by scanning the ``z+x_i`` 
 function over a list, given an initial state of ``z=0``. 

 Special cases:

    - A ``reduce()`` operation can be performed by returning only the last 
      output of a scan.
    
    - A ``map()`` operation can be performed by applying a function that 
      ignores each previous output.

 Often a for loop can be expressed as a scan() operation, and scan is the 
 closest that theano comes to looping.

 This module provides scanning functionality with the `Scan` Op.

"""
__docformat__ = 'restructedtext en'

import numpy 
import theano
from theano.tensor import opt
from theano import gof
from theano.compile import optdb

# Logging function for sending warning or info
import logging
_logger = logging.getLogger('theano.scan')
def warning(*msg):
    _logger.warning('WARNING theano.scan: '+' '.join(msg))
def info(*msg):
    _logger.info('INFO theano.scan: '+' '.join(msg))


# Hashing a dictionary or a list or a tuple or any type that is hashable with
# the hash() function
def hash_listsDictsTuples(x):
    hash_value = 0
    if type(x) == dict :
        for k,v in x.iteritems():
            hash_value ^= hash_listsDictsTuples(k)
            hash_value ^= hash_listsDictsTuples(v)
    elif type(x) in (list,tuple):
        for v in x:
            hash_value ^= hash_listsDictsTuples(v)
    else:
      try:
        hash_value ^= hash(x)
      except:
        pass
    return hash_value

def scan(fn, sequences, initial_states, non_sequences, inplace_map={}, 
         sequences_taps={}, outputs_taps = {},
         n_steps = theano.tensor.zero(), 
         truncate_gradient = -1, go_backwards = False, mode = 'FAST_RUN'):
    '''Function that constructs and applies a scan op

    :param fn: given variables representing all the slices of input and 
    past values of outputs and other non sequences parameters, fn should 
    produce variables describing the output of one time step of scan. 
    The order in which the argument to this function are given is very 
    important. You should have the following order: 
        * all time slices of the first sequence (as given in the ``sequences``
          list) ordered cronologically
        * all time slices of the second sequence (as given in the 
          ``sequences`` list) ordered cronologically
        ..
        * all time slices of the first output (as given in the 
          ``initial_state`` list) ordered cronologically 
        * all time slices of the second otuput (as given in the 
          ``initial_state`` list) ordered cronologically
        ...
        * all other parameters over which scan doesn't iterate given in 
          the same order as in ``non_sequences``
    The outputs of these function should have the same order as in the list
    ``initial_states`

    :param sequences: list of Theano variables over which scan needs to 
    iterate

    :param initial_states: list of Theano variables containing the initial
    state used for the output. Note that if the function applied recursively
    uses only the previous value of the output or none, this initial state 
    should have same shape as one time step of the output; otherwise, the 
    initial state should have the same number of dimension as output. This 
    can easily be understand through an example. For computing y(t) let 
    assume that we need y(t-1), y(t-2) y(t-4). Through an abuse of notation,
    when t = 0, we would need values for y(-1), y(-2) and y(-4). These values
    are provided by the initial state of y, which should have same number 
    of dimension as y, where the first dimension should be 4 in this case.
    If init_y is the initial values of y, then init_y[0] corresponds to 
    y[-4], init_y[1] corresponds to y[-3], init_y[2] corresponds to y[-2], 
    init_y[3] corresponds to y[-1]. By default, scan is set to use the 
    last time step for each output. 

    :param non_sequences: These are parameters used by the recursive function
    over which scan shouldn't iterate

    :param inplace_map: It is a dictionary where keys are output indexes, 
    and values are sequence indexes. Assigning to a key a value, means that 
    the output represented by key will be computed inplace (in the same 
    memory buffer) as the input represented by the value

    :param sequences_taps: At each step you can use different time slices 
    of sequences, and this dictionary lets you define exactly that. The 
    keys of the dictionary are sequence indexes, the values are list of 
    numbers. Having the following entry : i : [t_1,t_2,t_3], means that 
    at time step k, for sequence x, that has the index i in the list of 
    sequences, you would use the values  x[k+t_1], x[k+t_2], x[k+t_3]. 
    t_1, t_2, t_3 values can be positive or negative. If you do not want
    to use any time slice of the sequence you need to give to that entry
    the empy list. By default, for each entry the dictionary will contain
    the list [0].

    :param outputs_taps: This has the same meaning as the parameter 
    sequences_taps, with the only differences that these taps or for 
    the outputs, and that they have to be negative (smaller than 0). To 
    enforce an output to not use any past values, you have the specify 
    in the dictionary for that entry the emty list, otherwise, by default,
    scan will expect to use the last time step

    :param n_steps: in case you do not have any sequences over which you want 
    to iterate, but rather apply some set of computation for a number of 
    steps, or when you want to restrict to a certain length, you provide 
    that length as n_steps. It can be a theano scalar or a value. 

    :param truncate_gradient: if you compute gradients through a scan op,
    this can be computed using backpropagation through time. As such you 
    have the option to truncate the BPTT to a given number of steps (to 
    increase speed). If set to -1 no truncation is done.

    :param go_backwards: This give you the option to move backwards through
    your sequences instead of forward
    '''

    # check if inputs are just single variables instead of lists     
    if not (type(sequences) in (list, tuple)):
        seqs = [sequences]
    else:
        seqs = sequences
        
    if not (type(initial_states) in (list,tuple)):
        init_outs = [initial_states]
    else: 
        init_outs = initial_states
        
    if not (type(non_sequences) in (list,tuple)):
        non_seqs = [non_sequences]
    else:
        non_seqs = non_sequences



    # compute number of sequences and number of seqs   
    n_seqs     = len(seqs)
    n_outs   = len(init_outs)


    # update sequences_taps[idx] to contain 0 if it is not defined
    for i in xrange(n_seqs):
        if not sequences_taps.has_key(i):
            sequences_taps.update({i:[0]})
        # if input sequence is not actually used by the recursive function
        elif sequences_taps[i] == []:
            sequences_taps.__delitem__(i)
        elif not (type(sequences_taps[i]) in (list,tuple)):
            sequences_taps[i] = [sequences_taps[i]]
    # update outputs_taps[idx] to contain -1 if it is not defined
    for i in xrange(n_outs):
        if not outputs_taps.has_key(i):
            outputs_taps.update({i:[-1]})
        elif outputs_taps[i] == []:
            outputs_taps.__delitem__(i)
        elif not(type(outputs_taps[i]) in (list,tuple)):
            outputs_taps[i] = [outputs_taps[i]]
    '''
    # update keep_outputs list
    for i in xrange(n_outs):
        if not keep_outputs.has_key(i):
            keep_outputs[i] = True
        elif not keep_outputs[i]:
            if outputs_taps[i] != [-1]:
                keep_outputs[i] = True
                warning('You need to keep past value of outputs if you use'\
                        'past taps of output different from -1')
    '''
    keep_outputs = [ 0 for i in xrange(n_outs)]
                      



    # create theano inputs for the recursive function  
    args = []
    for (i,seq) in enumerate(seqs):
      if sequences_taps.has_key(i):
        for k in xrange(len(sequences_taps[i])):
            args += [seq[0].type() ]
    for (i,init_out) in enumerate(init_outs):
      if outputs_taps.has_key(i):
        for k in xrange(len(outputs_taps[i])):
            if outputs_taps[i] == [-1]:
                args += [init_out.type() ]
            else:
                args += [init_out[0].type() ]

    args      += non_seqs
    next_outs  = fn(*args)
    
    if not (type(next_outs) in (list,tuple)):
        next_outs = [next_outs]

    # Create the Scan op object
    local_op = Scan( (args,next_outs), n_seqs,n_outs,inplace_map,
            sequences_taps, outputs_taps, truncate_gradient,
            go_backwards, keep_outputs, mode)

    # Call the object on the input sequences, initial values for outs, 
    # and non sequences
    return local_op( *(    [theano.tensor.as_tensor(n_steps)]  \
                         + seqs \
                         + init_outs \
                         + non_seqs))




class Scan(theano.Op):
    def __init__(self,(inputs, outputs),n_seqs, n_outs,
                 inplace_map={}, seqs_taps={}, outs_taps={},
                 truncate_gradient = -1,
                 go_backwards = False, keep_outputs = {},
                 mode = 'FAST_RUN', inplace=False):
        '''
        :param (inputs,outputs): inputs and outputs Theano variables that 
        describe the function that is applied recursively 

        :param n_seqs: number of sequences over which scan will have to iterate

        :param n_outs: number of outputs of the scan op

        :param inplace_map: see scan function above

        :param seqs_taps: see scan function above

        :param outs_taps: see scan function above

        :param truncate_gradient: number of steps after which scan should truncate
        -1 implies no truncation 

        :param go_bacwards: see scan funcion above

        :param keep_outputs: a list of booleans of same size as the number of 
        outputs; the value at position ``i`` in the list corresponds to the 
        ``i-th`` output, and it tells how many steps (from the end towards 
        the begining) of the outputs you really need and should return; 
        given this information, scan can know (if possible) to allocate only
        the amount of memory needed to compute that many entries
        '''
        

        # check inplace map
        for _out,_in in inplace_map.iteritems():
            if _out > n_outs:
                raise ValueError(('Inplace map reffers to an unexisting'\
                          'output %d')% _out)
            if _in > n_seqs:
                raise ValueError(('Inplace map reffers to an unexisting'\
                          'input sequence %d')%_in)
            if (_in >= 0) and (min(seqs_taps[_in]) < 0):
                raise ValueError(('Input sequence %d uses past values that '\
                         'will be overwritten by inplace operation')%_in)


        #check sequences past taps
        for k,v in seqs_taps.iteritems():
          if k > n_seqs:
            raise ValueError(('Sequences past taps dictionary reffers to '
                    'an unexisting sequence %d')%k)

        #check outputs past taps
        for k,v in outs_taps.iteritems():
          if k > n_outs:
            raise ValueError(('Sequences past taps dictionary reffers to '
                    'an unexisting sequence %d')%k)
          if max(v) > -1:
            raise ValueError(('Can not require future value %d of output' \
                    ' %d')%(k,max(v)))



        self.destroy_map = {}
        if inplace:
            for i in inplace_map.keys():
                self.destroy_map.update({i: [inplace_map[i]] } )

        self.seqs_taps      = seqs_taps
        self.outs_taps      = outs_taps
        self.n_seqs         = n_seqs
        self.n_outs         = n_outs
        self.n_args         = n_seqs+n_outs+1
        self.inplace_map    = inplace_map
        self.keep_outputs   = keep_outputs
        self.inplace        = inplace
        self.inputs         = inputs
        self.outputs        = outputs
        self.truncate_gradient = truncate_gradient
        self.go_backwards   = go_backwards

        self.fn = theano.function(inputs,outputs, mode = mode)
        g_y = [outputs[0].type()]

        def compute_gradient(y, g_y):
            gmap = theano.gradient.grad_sources_inputs( \
                        [(y,g_y)], theano.gof.graph.inputs([y]), False)
            def zero(p):
              return theano.tensor.TensorConstant(theano.tensor.TensorType(\
                      dtype=p.type.dtype, broadcastable=[]),
                      theano._asarray(0,dtype = p.type.dtype))

            return [gmap.get(p, zero(p)) for p in inputs]


        g_args = compute_gradient( outputs[0], g_y[-1]) 
        # for all outputs compute gradients and then sum them up
        for y in outputs[1:]:
            g_y += [y.type()]
            g_args_y = compute_gradient( y,g_y[-1])
            for i in xrange(len(g_args)):
                g_args[i] += g_args_y[i]


        self.g_ins = g_y+inputs   
        self.g_outs = g_args


    def make_node(self,*inputs):
      n_args = len(inputs)
      if n_args < self.n_args :
         err = 'There should be at least '+str(self.n_args)+ 'arguments'
         raise ValueError(err)

      # Create list of output datatypes
      out_types = []
      for i in xrange(self.n_seqs+1, self.n_seqs+self.n_outs+1):
         if not (inputs[i] == []):
            if self.outs_taps.has_key(i-1-self.n_seqs) and \
               (self.outs_taps[i-self.n_seqs-1]==[-1]) and \
               (self.keep_outputs[i-1-self.n_seqs] != 1):
                out_types += [theano.tensor.Tensor(dtype=inputs[i].dtype, \
                   broadcastable=(False,)+inputs[i].broadcastable)()]
            elif not self.keep_outputs[i-1-self.n_seqs] == 1:
                out_types += [ inputs[i].type()]
            else:
                out_types += [theano.tensor.Tensor(dtype=inputs[i].dtype,\
                     broadcastable=(False,)+inputs[i].broadcastable[1:])()]
         else:
            raise ValueError(('You need to provide initial state for outputs'
                      ' such that scan can infer what dataype they are'))
      return theano.Apply(self,inputs, out_types)


    def __eq__(self,other):
      rval = type(self) == type(other)
      if rval:
        rval = (self.inputs == other.inputs) and \
               (self.outputs ==  other.outputs) and \
               (self.keep_outputs == other.keep_outputs) and \
               (self.g_ins == other.g_ins) and \
               (self.g_outs == other.g_outs) and \
               (self.seqs_taps == other.seqs_taps) and \
               (self.outs_taps == other.outs_taps) and \
               (self.inplace_map == other.inplace_map) and \
               (self.n_seqs == other.n_seqs) and\
               (self.inplace == other.inplace) and\
               (self.go_backwards == other.go_backwards) and\
               (self.truncate_gradient == other.truncate_gradient) and\
               (self.n_outs == other.n_outs) and\
               (self.n_args == other.n_args)
      return rval
      

    def __hash__(self):
      return hash(type(self)) ^ \
             hash(self.n_seqs) ^ \
             hash(self.n_outs) ^ \
             hash(self.inplace) ^\
             hash(self.go_backwards) ^\
             hash(self.truncate_gradient) ^\
             hash(self.n_args) ^ \
             hash_listsDictsTuples(self.outputs) ^ \
             hash_listsDictsTuples(self.inputs) ^ \
             hash_listsDictsTuples(self.g_ins) ^ \
             hash_listsDictsTuples(self.g_outs) ^ \
             hash_listsDictsTuples(self.seqs_taps) ^\
             hash_listsDictsTuples(self.outs_taps) ^\
             hash_listsDictsTuples(self.keep_outputs)




    def perform(self,node,args, outs):

        n_steps = 0 
        if (self.n_seqs ==0 ) and (args[0] == 0):
            raise ValueError('Scan does not know over how many steps it '
                'should iterate! No input sequence or number of steps to '
                'iterate given !')

        if (args[0] != 0):
            n_steps = args[0]
        
        for i in xrange(self.n_seqs):
          if self.seqs_taps.has_key(i):
              # compute actual length of the sequence ( we need to see what
              # past taps this sequence has, and leave room for them 
              seq_len = args[i+1].shape[0] + min(self.seqs_taps[i])
              if  max( self.seqs_taps[i]) > 0: 
                  # using future values, so need to end the sequence earlier
                  seq_len -= max(self.seqs_taps[i])
              if n_steps == 0 :
                  # length of the sequences, leaving room for the largest
                  n_steps = seq_len
              if seq_len != n_steps : 
                  warning(('Input sequence %d has a shorter length then the '
                          'expected number of steps %d')%(i,n_steps))
                  n_steps = min(seq_len,n_steps)



        # check if we deal with an inplace operation 
        inplace_map  = self.inplace_map
        if not self.inplace: #if it was not optimized to work inplace
            inplace_map = {}

 
        # check lengths of init_outs
        for i in xrange(self.n_seqs+1, \
                        self.n_seqs+self.n_outs+1):
          if self.outs_taps.has_key(i-self.n_seqs-1):
            if self.outs_taps[i-self.n_seqs-1] == [-1]:
                args[i] = numpy.array([args[i]])

            req_size = abs(min(self.outs_taps[i-self.n_seqs-1]))-1
            if args[i].shape[0] < req_size:
              warning(('Initial state for output %d has fewer values then '
                 'required by the maximal past value %d. Scan will use 0s'
                 ' for missing values')%(i-self.n_iterable-1,req_size))
            
        self.n_steps = n_steps
        y = self.scan(self.fn, args[1:],self.n_seqs, self.n_outs, 
                 self.seqs_taps, self.outs_taps, n_steps, self.go_backwards, 
                 inplace_map)


        # write to storage
        for i in xrange(self.n_outs):
            outs[i][0]=y[i]



    def scan(self,fn, args, n_seqs, n_outs, seqs_taps, outs_taps,  n_steps, 
             go_backwards, inplace_map):

      y = []
      for i in xrange(n_outs):
        if inplace_map.has_key(i) and (inplace_map[i] >= 0):
          y += [args[inplace_map[i]]]
        else:
          if self.keep_outputs[i] < 1 :
              y_shape = (n_steps,)+args[i+n_seqs].shape[1:]
          elif self.keep_outputs[i] == 1:
              y_shape = args[i+n_seqs].shape[1:]
          else:
              y_shape = (self.keep_outputs[i],)+args[i+n_seqs].shape[1:]


          y += [numpy.empty(y_shape,
                            dtype=args[i+n_seqs].dtype)]
      seqs_mins = {}
      for j in xrange(n_seqs):
        if seqs_taps.has_key(j):
          seqs_mins.update({j:  min(seqs_taps[j])})

      outs_mins = {}
      initOuts_size = {}
      for j in xrange(n_outs):
        if outs_taps.has_key(j):
          outs_mins.update({j: min(outs_taps[j])})
          initOuts_size.update({j: args[n_seqs+j].shape[0]})


      for i in xrange(n_steps):
        fn_args = []

        # sequences over which scan iterates
        # check to see if we are scaning them backwards or no
        _i = i
        if go_backwards:
            _i = n_steps-1-i
        for j in xrange(n_seqs):
          if seqs_taps.has_key(j):
            ls_taps = seqs_taps[j]
            min_tap = seqs_mins[j]
            for tap_value in ls_taps:
                k = _i - min_tap + tap_value
                fn_args += [args[j][k]]


        # past values of outputs
        for j in xrange(n_outs):
          if outs_taps.has_key(j):
            ls_taps = outs_taps[j]
            min_tap = outs_mins[j]
            sz = initOuts_size[j]
            for tap_value in ls_taps:
              if i + tap_value < 0:
                k = i + sz + tap_value
                if k < 0:
                  # past value not provided.. issue a warning and use 0s
                  fn_args += [numpy.zeros(args[j+n_seqs][0].shape)]
                  warning(('Past value %d for output %d not given in inital '
                           'out') % (j,tap_value))
                else:
                  fn_args += [args[j+n_seqs][k]]
              else:
                if self.keep_outputs[j] < 1:
                    fn_args += [y[j][i + tap_value]]
                elif self.keep_outputs[j] == 1:
                    fn_args += [y[j] ]
                else:
                    raise NotImplementedError('in the near future')
        # get the non-iterable sequences
        fn_args += list(args[(n_seqs+n_outs):])
        # compute output
        something = fn(*fn_args)
        #update outputs
        for j in xrange(n_outs):
          if self.keep_outputs[j] <1:
              y[j][i] = something[j]
          elif self.keep_outputs[j] == 1:
              y[j] = something[j]
          else:
            raise NotImplementedError('in the near future')
      return y


    def grad(self, args, g_outs):
        if True: 
           #((self.updates.keys() != []) or (self.inplace_map.keys() != [])\
           # or numpy.any(self.keep_outputs)):
           # warning('Can not compute gradients if inplace or updates ' \
           #         'are used or if you do not keep past value of outputs.'\
           #         'Use force_gradient if you know for sure '\
           #         'that the gradient can be computed automatically.')
           warning('Gradient not fully tested yet !')         
           return [None for i in args]
        else:
            # forward pass 
            y = self(*args)
            if not( type(y) in (list,tuple)):
                y = [y]
 

            # backwards pass
            for i in xrange(len(y)):
               if g_outs[i] == None:
                  g_outs[i] = theano.tensor.zeros_like(y[i])

            g_args = [self.n_steps]+g_outs + y 
            # check if go_backwards is true
            if self.go_backwards:
               for seq in args[1:self.n_seqs]:
                 g_args += [seq[::-1]]
            else:
               g_args += args[1:self.n_seqs] 

            g_args += args[1+self.n_seqs: ]


            g_scan = ScanGrad((self.g_ins,self.g_outs), self.n_seqs, \
                              self.n_outs,self.seqs_taps, self.outs_taps,
                              self.truncate_gradient)

            return g_scan(g_args)



@gof.local_optimizer([None])
def scan_make_inplace(node):
    op = node.op
    if isinstance(op, Scan) and (not op.inplace) \
                            and (op.inplace_map.keys() != []):
        return Scan((op.inputs, op.outputs) , op.n_seqs,  \
                    op.n_outs, op.inplace_map, op.seqs_taps, op.outs_taps, \
                    op.force_gradient, op.truncate_gradient, \
                    op.go_backwards, inplace=True \
                      ).make_node(*node.inputs).outputs
    return False
        
        
optdb.register('scan_make_inplace', opt.in2out(scan_make_inplace,\
               ignore_newtrees=True), 75, 'fast_run', 'inplace')




class ScanGrad(theano.Op):
    """Gradient Op for Scan"""
    def __init__(self,(g_ins, g_outs) , n_seqs, n_outs, 
                 seqs_taps = {}, outs_taps= {}, truncate_gradient = -1):
        self.grad_fn = theano.function(g_ins, g_outs)
        self.inputs = g_ins
        self.outputs = g_outs
        self.n_seqs = n_seqs
        self.truncate_gradient = truncate_gradient
        self.n_outs = n_outs
        self.seqs_taps = seqs_taps
        self.outs_taps = outs_taps
        self.destroy_map = {}


    def __eq__(self,other): 
        rval = type(self) == type(other)
        if rval:
           rval = (self.inputs == other.inputs) and \
                  (self.outputs == other.outputs) and \
                  (self.n_seqs == other.n_seqs) and \
                  (self.n_outs == other.n_outs) and \
                  (self.truncate_gradient == other.truncate_gradient) and\
                  (self.seqs_taps == other.seqs_taps) and \
                  (self.outs_taps == other.outs_taps) 
        return rval

    def __hash__(self):
        return hash(type(self)) ^ \
               hash(self.n_seqs) ^ \
               hash(self.n_outs) ^ \
               hash(self.truncate_gradient) ^\
               hash_list(self.inputs) ^ \
               hash_list(self.outputs) ^ \
               hash_dict(self.seqs_taps) ^ \
               hash_dict(self.outs_taps)

    def make_node(self, *args):
        # input of the gradient op : 
        # | g_outs | y      | seqs   | outs    | non_seqs   |
        # | n_outs | n_outs | n_seqs | n_outs  | unknown    |
        # return 
        # | grad of seqs | grad of outs | grad of non_seqs  |
        # |   n_seqs     |  n_outs      |  unknown          |
        return theano.Apply(self, list(args),
                    [i.type() for i in args[1+2*self.n_outs:] ])

    def perform(self, node, args, storage):
            # get scan inputs
            n_steps = args[0]
            inputs = args[2*self.n_outs+1:]
            seqs = inputs[:self.n_seqs]
            seeds = inputs[self.n_seqs:self.n_seqs+self.n_outs]
            non_seqs = inputs[self.n_outs+self.n_seqs:]
            
            # generate space for gradient 
            g_seqs     = [numpy.zeros_like(k) for k in seqs]
            g_seeds    = [numpy.zeros_like(k) for k in seeds]
            g_non_seqs = [numpy.zeros_like(k) for k in non_seqs]
            # get gradient from above
            g_outs = args[:self.n_outs]

            # get the output of the scan operation
            outs = args[self.n_outs:2*self.n_outs]


            # go back through time to 0 or n_steps - truncate_gradient
            lower_limit = n_steps - self.truncate_gradient
            if lower_limit > n_steps-1:
                the_range = xrange(n_steps-1,-1,-1)
            elif lower_limit < -1:
                the_range = xrange(n_steps-1,-1,-1)
            else:
                the_range = xrange(n_steps-1, lower_limit,-1)



            seqs_mins = {}
            for j in xrange(self.n_seqs):
              if self.seqs_taps.has_key(j):
                seqs_mins.update({j: min(self.seqs_taps[j])})

            outs_mins = {}
            seed_size = {}
            for j in xrange(self.n_outs):
              if self.outs_taps.has_key(j):
                outs_mins.update({j: min(self.outs_taps[j])})
                seed_size.update({j: g_seeds[j].shape[0]})

            for i in the_range:
              # time slice of inputs
              _ins = []
              for j in xrange(self.n_seqs):
                if self.seqs_taps.has_key(j):
                  ls_taps = self.seqs_taps[j] 
                  min_tap =      seqs_mins[j]
                  for tap_value in ls_taps:
                    k = i - min_tap + tap_value
                    _ins += [ins[j][k]]
              # time slice of outputs + taps
              _outs = []
              for j in xrange(self.n_outs):
                if self.outs_taps.has_key(j):
                  ls_taps = self.outs_taps[j]
                  min_tap =      outs_mins[j]
                  seed_sz =      seed_size[j]
                  for tap_value in ls_taps:
                    if i + tap_value < 0:
                      k = i + seed_sz  + tap_value
                      if k < 0 :
                        #past value not provided .. issue a warning and use 0
                        _outs += [numpy.zeros(seeds[j][0].shape)]
                        warning('Past value %d for output $d not given' \
                              %(j,tap_value))
                      else:
                        _outs += [seeds[j][k]]
                    else:
                      _outs += [outs[j][i + tap_value]]

              g_out = [arg[i] for arg in g_outs]
              grad_args = g_out + _ins + _outs + non_seqs
              grads=self.grad_fn(*grad_args)
 
              # get gradient for inputs 
              pos = 0
              for j in xrange(self.n_seqs):
                if self.seqs_taps.has_key(j):
                  ls_taps = self.seqs_taps[j]
                  min_tap =      seqs_mins[j]
                  for tap_value in ls_taps :
                    k = i - min_tap + tap_value
                    g_ins[j][k] += grads[pos]
                    pos += 1


              # get gradient for outputs
              for j in xrange(self.n_outs):
                if self.outs_taps.has_key(j):
                  ls_taps = self.outs_taps[j]
                  min_tap =      outs_mins[j]
                  seed_sz =      seed_size[j]
                  for tap_value in ls_taps:
                    if i+tap_value < 0 :
                     k = i + seed_sz + tap_value
                     if  k > 0 :
                        g_seeds[j][k] += grads[pos]
                        pos += 1
              for j in xrange(len(g_non_seqs)):
                g_non_seqs[j] += grads[j+pos]


            # return the gradient

            for i,v in enumerate(g_ins + g_seeds+ g_non_seqs):
                storage[i][0] = v




