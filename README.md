tracker.py runs single cored so its a bit less pc killing.

parallel.py is a more recent version, coded to basically do the same thing but faster and utilize more threads.
you should only need to do captcha once every couple times, but the multi core does this in the form of multi selenium instances so you may have to do captcha like x times where x is the number of browser workers.
at least from my experience anything more than 6 threads doesnt make a huge difference on the amex site since it runs so quickly anyways.
